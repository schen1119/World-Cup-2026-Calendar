#!/usr/bin/env python3
"""
Generates a subscribable .ics calendar for the 2026 FIFA World Cup group stage.

Each match's description includes the group standings as they stood entering
that specific kickoff (computed progressively from finished results, not just
"today's" table) -- so the file stays historically accurate even as it's
regenerated over and over throughout the tournament.

Data source: football-data.org API v4 (https://www.football-data.org/).
Requires an API key in the FOOTBALL_DATA_API_KEY environment variable.

USAGE:
    FOOTBALL_DATA_API_KEY=xxxx python3 scripts/generate_calendar.py
"""

import os
import sys
import hashlib
import datetime
from collections import defaultdict

import requests

BASE_URL = "https://api.football-data.org/v4"
COMPETITION_CODE = "WC"
OUTPUT_ICS  = "docs/world-cup-2026-group-stage.ics"
OUTPUT_HTML = "docs/index.html"

FINISHED_STATUSES = {"FINISHED", "AWARDED"}


def now_eastern():
    """Return current time as a timezone-aware Eastern datetime, honouring DST."""
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    year = utc_now.year
    # 2nd Sunday in March at 07:00 UTC = DST start
    march1 = datetime.datetime(year, 3, 1, tzinfo=datetime.timezone.utc)
    dst_start = march1 + datetime.timedelta(days=(6 - march1.weekday()) % 7 + 7, hours=7)
    # 1st Sunday in November at 06:00 UTC = DST end
    nov1 = datetime.datetime(year, 11, 1, tzinfo=datetime.timezone.utc)
    dst_end = nov1 + datetime.timedelta(days=(6 - nov1.weekday()) % 7, hours=6)
    if dst_start <= utc_now < dst_end:
        tz = datetime.timezone(datetime.timedelta(hours=-4), "EDT")
    else:
        tz = datetime.timezone(datetime.timedelta(hours=-5), "EST")
    return utc_now.astimezone(tz)


def as_of_string():
    """Human-readable timestamp in US Eastern, e.g. '6/21/2026 at 3:45 PM EDT'."""
    et = now_eastern()
    tz_name = et.tzname()
    # %-m / %-I strips leading zeros on Linux (works fine in GitHub Actions)
    return et.strftime(f"%-m/%-d/%Y at %-I:%M %p {tz_name}")

# Candidate season years to try if the primary one returns nothing.
# football-data.org sometimes keys a tournament by the year it starts
# rather than the year the final is played (which would be 2026).
SEASON_CANDIDATES = [2026, 2025]


def get_api_key():
    key = os.environ.get("FOOTBALL_DATA_API_KEY")
    if not key:
        sys.exit("ERROR: FOOTBALL_DATA_API_KEY environment variable is not set.")
    return key


def api_get(path, params, api_key):
    headers = {"X-Auth-Token": api_key}
    resp = requests.get(f"{BASE_URL}/{path}", headers=headers, params=params, timeout=30)
    if resp.status_code == 403:
        # Return None rather than crashing -- caller decides how to handle
        print(f"  WARNING: 403 on /{path} (plan may not include this endpoint or season) -- skipping.")
        return None
    if resp.status_code == 429:
        sys.exit("ERROR: Rate limit hit (429). Wait a minute and retry.")
    resp.raise_for_status()
    return resp.json()


def find_working_season(api_key):
    """Try each season candidate and return (season, matches_data) for the
    first one that yields group-stage matches. Exits if none work."""
    for season in SEASON_CANDIDATES:
        print(f"  Trying season={season}...")
        data = api_get(f"competitions/{COMPETITION_CODE}/matches", {"season": season}, api_key)
        if data is None:
            continue
        matches = data.get("matches", [])
        group_matches = [m for m in matches if "GROUP" in (m.get("stage") or "").upper()]
        if group_matches:
            print(f"  Found {len(group_matches)} group-stage matches under season={season}.")
            return season, matches
        else:
            print(f"  No GROUP_STAGE matches found under season={season}.")
    sys.exit(
        "ERROR: Could not find group-stage match data under any season candidate. "
        "Check that your API plan includes the World Cup competition, or inspect "
        f"https://api.football-data.org/v4/competitions/{COMPETITION_CODE}/matches manually."
    )


def fetch_team_to_group_from_standings(api_key, season):
    """Returns {team_id: 'GROUP_A', ...} from the /standings endpoint.
    Returns {} (empty dict) gracefully if the endpoint is not accessible on
    the current plan -- group membership is derived from match data instead."""
    print("  Fetching /standings...")
    data = api_get(f"competitions/{COMPETITION_CODE}/standings", {"season": season}, api_key)
    if data is None:
        print("  Standings endpoint not available on this plan -- will derive groups from match data instead.")
        return {}
    team_group = {}
    for group_table in data.get("standings", []):
        grp = group_table.get("group")
        if not grp:
            continue
        for row in group_table.get("table", []):
            team_group[row["team"]["id"]] = grp
    if not team_group:
        print("  Standings returned no group data -- will derive groups from match data instead.")
    return team_group


def derive_team_to_group_from_matches(matches):
    """Fallback: build {team_id: group} directly from match objects.
    football-data.org includes a 'group' field on each GROUP_STAGE match."""
    team_group = {}
    for m in matches:
        if "GROUP" not in (m.get("stage") or "").upper():
            continue
        grp = m.get("group")
        if not grp:
            continue
        for side in ("homeTeam", "awayTeam"):
            tid = m[side]["id"]
            if tid not in team_group:
                team_group[tid] = grp
    return team_group


def group_letter(group_name):
    # football-data.org returns e.g. "GROUP_A" -> normalize to just "A"
    return group_name.replace("GROUP_", "").replace("Group ", "").strip()


def new_team_stats():
    return {"P": 0, "W": 0, "D": 0, "L": 0, "GF": 0, "GA": 0, "GD": 0, "Pts": 0}


def sort_table(stats):
    return sorted(stats.items(), key=lambda kv: (-kv[1]["Pts"], -kv[1]["GD"], -kv[1]["GF"], kv[0]))


def standings_block(group, stats, md_label, as_of_str):
    table = sort_table(stats)
    lines = [f"Group {group} Standings entering this match ({md_label}):"]
    for i, (team, s) in enumerate(table, start=1):
        if s["P"] == 0:
            lines.append(f"{i}. {team} \u2014 0 pts (group play has not started)")
        else:
            gd_str = f"+{s['GD']}" if s["GD"] > 0 else str(s["GD"])
            pt_word = "pt" if s["Pts"] == 1 else "pts"
            lines.append(
                f"{i}. {team} \u2014 {s['W']}W {s['D']}D {s['L']}L \u2014 "
                f"{s['Pts']} {pt_word} (GD {gd_str}, {s['GF']} GF)"
            )
    lines.append(
        f"(Last updated {as_of_str} \u2014 reflects results up to this match's kickoff)"
    )
    return "\n".join(lines)


def stable_uid(group, home, away):
    key = f"{group}-{home}-{away}".lower().replace(" ", "")
    h = hashlib.md5(key.encode()).hexdigest()[:16]
    return f"{h}@worldcup2026-group-stage"


def fmt_ics_dt(dt):
    return dt.strftime("%Y%m%dT%H%M%SZ")


def escape_ics_text(text):
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def build_standings_page(matches, team_group, as_of_str):
    """
    Returns an HTML string showing the current (fully up-to-date) group
    standings table for all groups. Designed to be served as docs/index.html
    via GitHub Pages alongside the .ics feed.
    """
    # Diagnostic: show what stage labels the API is actually returning
    stage_counts = {}
    for m in matches:
        s = m.get("stage", "MISSING")
        stage_counts[s] = stage_counts.get(s, 0) + 1
    print(f"  [HTML] Stage labels in API response: {stage_counts}")

    # Accept any stage label that looks like the group stage.
    # football-data.org has used both "GROUP_STAGE" and "Groups" in the past.
    def is_group_stage(match):
        stage = (match.get("stage") or "").upper()
        return "GROUP" in stage

    # Compute final standings from ALL finished matches
    by_group = defaultdict(lambda: defaultdict(new_team_stats))

    for match in matches:
        if not is_group_stage(match):
            continue
        home = match["homeTeam"]
        away = match["awayTeam"]
        raw_group = match.get("group") or team_group.get(home["id"]) or team_group.get(away["id"])
        if not raw_group:
            continue
        letter = group_letter(raw_group)
        # Pre-initialize so all 4 teams appear even before playing
        if home["name"] not in by_group[letter]:
            by_group[letter][home["name"]] = new_team_stats()
        if away["name"] not in by_group[letter]:
            by_group[letter][away["name"]] = new_team_stats()

        status = match.get("status")
        full_time = (match.get("score") or {}).get("fullTime") or {}
        hg = full_time.get("home")
        ag = full_time.get("away")
        if status in FINISHED_STATUSES and hg is not None and ag is not None:
            sh = by_group[letter][home["name"]]
            sa = by_group[letter][away["name"]]
            sh["P"] += 1; sa["P"] += 1
            sh["GF"] += hg; sh["GA"] += ag
            sa["GF"] += ag; sa["GA"] += hg
            sh["GD"] = sh["GF"] - sh["GA"]
            sa["GD"] = sa["GF"] - sa["GA"]
            if hg > ag:
                sh["W"] += 1; sh["Pts"] += 3; sa["L"] += 1
            elif hg < ag:
                sa["W"] += 1; sa["Pts"] += 3; sh["L"] += 1
            else:
                sh["D"] += 1; sh["Pts"] += 1; sa["D"] += 1; sa["Pts"] += 1

    print(f"  [HTML] Groups found: {sorted(by_group.keys()) or 'NONE — group data missing from API response'}")

    # Build group HTML blocks
    group_blocks = []
    for letter in sorted(by_group.keys()):
        table = sort_table(dict(by_group[letter]))
        rows_html = ""
        for rank, (team, s) in enumerate(table, start=1):
            gd_str = f"+{s['GD']}" if s["GD"] > 0 else str(s["GD"])
            qualifier = " qualifier" if rank <= 2 else ""
            rows_html += (
                f'<tr class="row{qualifier}">'
                f'<td class="rank">{rank}</td>'
                f'<td class="team">{team}</td>'
                f'<td>{s["P"]}</td>'
                f'<td>{s["W"]}</td>'
                f'<td>{s["D"]}</td>'
                f'<td>{s["L"]}</td>'
                f'<td>{s["GF"]}</td>'
                f'<td>{s["GA"]}</td>'
                f'<td class="gd">{gd_str}</td>'
                f'<td class="pts">{s["Pts"]}</td>'
                f'</tr>\n'
            )
        group_blocks.append(f"""
    <div class="group">
      <h2>Group {letter}</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th class="team">Team</th>
            <th title="Played">P</th>
            <th title="Won">W</th>
            <th title="Drawn">D</th>
            <th title="Lost">L</th>
            <th title="Goals For">GF</th>
            <th title="Goals Against">GA</th>
            <th title="Goal Difference">GD</th>
            <th title="Points">Pts</th>
          </tr>
        </thead>
        <tbody>
{rows_html}        </tbody>
      </table>
    </div>""")

    if group_blocks:
        groups_html = "\n".join(group_blocks)
    else:
        groups_html = '<p class="no-data">Standings data is not yet available from the API.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>FIFA World Cup 2026 — Group Stage Standings</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0a0f1e;
      color: #e8eaf0;
      min-height: 100vh;
      padding: 2rem 1rem 4rem;
    }}

    header {{
      text-align: center;
      margin-bottom: 2.5rem;
    }}

    header h1 {{
      font-size: clamp(1.4rem, 4vw, 2.2rem);
      font-weight: 700;
      letter-spacing: 0.02em;
      color: #ffffff;
    }}

    header h1 span {{
      color: #f5a623;
    }}

    .updated {{
      margin-top: 0.5rem;
      font-size: 0.8rem;
      color: #7a8099;
    }}

    .subscribe {{
      display: inline-block;
      margin-top: 1.2rem;
      padding: 0.5rem 1.2rem;
      background: #1a6ef5;
      color: #fff;
      border-radius: 6px;
      text-decoration: none;
      font-size: 0.85rem;
      font-weight: 600;
      letter-spacing: 0.03em;
      transition: background 0.15s;
    }}
    .subscribe:hover {{ background: #1558cc; }}

    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 520px), 1fr));
      gap: 1.5rem;
      max-width: 1200px;
      margin: 0 auto;
    }}

    .group {{
      background: #131929;
      border: 1px solid #1e2740;
      border-radius: 10px;
      overflow: hidden;
    }}

    .group h2 {{
      padding: 0.75rem 1rem;
      font-size: 0.9rem;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #f5a623;
      background: #0e1424;
      border-bottom: 1px solid #1e2740;
    }}

    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
    }}

    thead tr {{
      background: #0e1424;
    }}

    th {{
      padding: 0.45rem 0.5rem;
      text-align: center;
      font-size: 0.7rem;
      font-weight: 600;
      letter-spacing: 0.06em;
      color: #7a8099;
      text-transform: uppercase;
      cursor: default;
    }}
    th.team {{ text-align: left; padding-left: 0.75rem; }}

    td {{
      padding: 0.5rem 0.5rem;
      text-align: center;
      border-top: 1px solid #1a2035;
    }}
    td.team {{ text-align: left; padding-left: 0.75rem; font-weight: 500; }}
    td.rank {{ color: #7a8099; font-size: 0.75rem; }}
    td.pts  {{ font-weight: 700; color: #ffffff; }}
    td.gd   {{ color: #a0aabf; }}

    tr.row.qualifier {{ background: rgba(26, 110, 245, 0.08); }}
    tr.row:hover     {{ background: #1a2240; }}

    .no-data {{
      grid-column: 1 / -1;
      text-align: center;
      padding: 3rem 1rem;
      color: #4a5270;
      font-size: 0.9rem;
    }}

    .qualifier-note {{      text-align: center;
      margin-top: 1rem;
      font-size: 0.72rem;
      color: #4a5270;
    }}
    .qualifier-note span {{
      display: inline-block;
      width: 10px; height: 10px;
      background: rgba(26, 110, 245, 0.3);
      border: 1px solid rgba(26, 110, 245, 0.5);
      border-radius: 2px;
      margin-right: 4px;
      vertical-align: middle;
    }}

    footer {{
      text-align: center;
      margin-top: 3rem;
      font-size: 0.72rem;
      color: #4a5270;
    }}
  </style>
</head>
<body>
  <header>
    <h1>FIFA World Cup 2026 <span>Group Stage</span></h1>
    <p class="updated">Last updated: {as_of_str}</p>
    <a class="subscribe"
       href="webcal://YOUR-USERNAME.github.io/YOUR-REPO/world-cup-2026-group-stage.ics">
      &#x1F4C5; Subscribe to Calendar
    </a>
  </header>

  <div class="grid">
{groups_html}
  </div>

  <p class="qualifier-note">
    <span></span>Top 2 teams in each group advance to the Round of 32.
    Tiebreaker order: points → goal difference → goals scored → head-to-head.
  </p>

  <footer>
    Data via <a href="https://www.football-data.org" style="color:#4a5270">football-data.org</a>
    &nbsp;·&nbsp; Auto-updated via GitHub Actions
  </footer>
</body>
</html>
"""


def build_calendar(matches, team_group, as_of_str):
    by_group = defaultdict(list)

    def is_group_stage(match):
        stage = (match.get("stage") or "").upper()
        return "GROUP" in stage

    for match in matches:
        if not is_group_stage(match):
            continue

        home = match["homeTeam"]
        away = match["awayTeam"]

        raw_group = match.get("group")
        if not raw_group:
            raw_group = team_group.get(home["id"]) or team_group.get(away["id"])
        if not raw_group:
            continue
        letter = group_letter(raw_group)

        md = match.get("matchday")

        kickoff = datetime.datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
        kickoff_utc = kickoff.astimezone(datetime.timezone.utc).replace(tzinfo=None)

        venue = match.get("venue") or "TBD"

        status = match.get("status")
        full_time = (match.get("score") or {}).get("fullTime") or {}
        goals_home = full_time.get("home")
        goals_away = full_time.get("away")
        score = (
            (goals_home, goals_away)
            if status in FINISHED_STATUSES
            and goals_home is not None
            and goals_away is not None
            else None
        )

        by_group[letter].append({
            "matchday": md,
            "kickoff": kickoff_utc,
            "home": home["name"],
            "away": away["name"],
            "venue": venue,
            "score": score,
        })

    for letter in by_group:
        by_group[letter].sort(key=lambda m: (m["matchday"] or 0, m["kickoff"]))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//World Cup 2026 Auto-Updater//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:FIFA World Cup 2026 - Group Stage (Live)",
        "X-WR-CALDESC:Auto-updated group stage schedule and standings, refreshed via football-data.org.",
        "X-WR-TIMEZONE:UTC",
    ]

    now_stamp = fmt_ics_dt(datetime.datetime.utcnow())
    event_count = 0

    for letter, glist in sorted(by_group.items()):
        stats = {}
        for m in glist:
            stats.setdefault(m["home"], new_team_stats())
            stats.setdefault(m["away"], new_team_stats())

        matchdays = sorted(set(m["matchday"] for m in glist if m["matchday"]))
        for md_num in matchdays:
            md_matches = [m for m in glist if m["matchday"] == md_num]
            md_label = "before Matchday 1" if md_num == 1 else f"after Matchday {md_num - 1}"
            desc_standings = standings_block(letter, stats, md_label, as_of_str)

            for m in md_matches:
                end = m["kickoff"] + datetime.timedelta(hours=2)
                uid = stable_uid(letter, m["home"], m["away"])
                summary = f"Group {letter}: {m['home']} vs {m['away']}"
                location = m["venue"]
                desc = (
                    f"FIFA World Cup 2026 Group Stage - Group {letter}\n"
                    f"{m['home']} vs {m['away']}\n"
                    f"Venue: {location}\n\n"
                    f"{desc_standings}"
                )
                lines += [
                    "BEGIN:VEVENT",
                    f"UID:{uid}",
                    f"DTSTAMP:{now_stamp}",
                    f"DTSTART:{fmt_ics_dt(m['kickoff'])}",
                    f"DTEND:{fmt_ics_dt(end)}",
                    f"SUMMARY:{escape_ics_text(summary)}",
                    f"LOCATION:{escape_ics_text(location)}",
                    f"DESCRIPTION:{escape_ics_text(desc)}",
                    "END:VEVENT",
                ]
                event_count += 1

            for m in md_matches:
                if m["score"] is not None:
                    hg, ag = m["score"]
                    sh, sa = stats[m["home"]], stats[m["away"]]
                    sh["P"] += 1; sa["P"] += 1
                    sh["GF"] += hg; sh["GA"] += ag
                    sa["GF"] += ag; sa["GA"] += hg
                    sh["GD"] = sh["GF"] - sh["GA"]
                    sa["GD"] = sa["GF"] - sa["GA"]
                    if hg > ag:
                        sh["W"] += 1; sh["Pts"] += 3; sa["L"] += 1
                    elif hg < ag:
                        sa["W"] += 1; sa["Pts"] += 3; sh["L"] += 1
                    else:
                        sh["D"] += 1; sh["Pts"] += 1
                        sa["D"] += 1; sa["Pts"] += 1

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n", event_count


def main():
    api_key = get_api_key()
    as_of_str = as_of_string()   # e.g. "6/21/2026 at 3:45 PM EDT"

    print("Step 1: Finding the correct season and fetching matches...")
    season, matches = find_working_season(api_key)

    print("Step 2: Fetching group assignments...")
    team_group = fetch_team_to_group_from_standings(api_key, season)
    if not team_group:
        print("  Falling back to deriving group assignments from match data...")
        team_group = derive_team_to_group_from_matches(matches)
    if not team_group:
        sys.exit(
            "ERROR: Could not determine group assignments from either standings or match data. "
            "The API may not be returning group information for this competition/season yet."
        )
    print(f"  Found {len(set(team_group.values()))} groups covering {len(team_group)} teams.")

    print("Step 3: Building calendar (.ics)...")
    ics_text, event_count = build_calendar(matches, team_group, as_of_str)

    if event_count == 0:
        sys.exit(
            "ERROR: Built 0 events. Group-stage matches were found but could not be "
            "processed -- check that the 'group' field is present on match objects."
        )

    os.makedirs(os.path.dirname(OUTPUT_ICS), exist_ok=True)
    with open(OUTPUT_ICS, "w") as f:
        f.write(ics_text)
    print(f"  Wrote {event_count} events to {OUTPUT_ICS}")

    print("Step 4: Building standings page (index.html)...")
    html = build_standings_page(matches, team_group, as_of_str)
    with open(OUTPUT_HTML, "w") as f:
        f.write(html)
    print(f"  Wrote standings page to {OUTPUT_HTML}")

    print(f"\nDone (season={season}, as of {as_of_str}).")


if __name__ == "__main__":
    main()
