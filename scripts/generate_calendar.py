#!/usr/bin/env python3
"""
Generates a subscribable .ics calendar for the 2026 FIFA World Cup group stage.

Each match's description includes the group standings as they stood entering
that specific kickoff (computed progressively from finished results, not just
"today's" table) -- so the file stays historically accurate even as it's
regenerated over and over throughout the tournament.

Data source: football-data.org API v4 (https://www.football-data.org/).
Requires an API key (their "token") in the FOOTBALL_DATA_API_KEY environment
variable.

USAGE:
    FOOTBALL_DATA_API_KEY=xxxx python3 scripts/generate_calendar.py

NOTE: COMPETITION_CODE below ("WC") is football-data.org's stable code for
the FIFA World Cup. Worth a quick sanity check against
https://api.football-data.org/v4/competitions on your own account to confirm
your plan tier includes it before relying on this long-term.
"""

import os
import sys
import hashlib
import datetime
from collections import defaultdict

import requests

BASE_URL = "https://api.football-data.org/v4"
COMPETITION_CODE = "WC"     # football-data.org's code for "FIFA World Cup"
SEASON = 2026
OUTPUT_PATH = "docs/world-cup-2026-group-stage.ics"   # served via GitHub Pages from /docs

# football-data.org match statuses considered "finished"
FINISHED_STATUSES = {"FINISHED", "AWARDED"}


def get_api_key():
    key = os.environ.get("FOOTBALL_DATA_API_KEY")
    if not key:
        sys.exit("ERROR: FOOTBALL_DATA_API_KEY environment variable is not set.")
    return key


def api_get(path, params, api_key):
    headers = {"X-Auth-Token": api_key}
    resp = requests.get(f"{BASE_URL}/{path}", headers=headers, params=params, timeout=30)
    if resp.status_code == 429:
        sys.exit("ERROR: Hit football-data.org's rate limit (429). Try again shortly.")
    resp.raise_for_status()
    return resp.json()


def fetch_team_to_group(api_key):
    """Returns {team_id: 'Group A', ...} from the standings endpoint.
    Used as a fallback when a match object doesn't carry its own 'group' field."""
    data = api_get(f"competitions/{COMPETITION_CODE}/standings", {"season": SEASON}, api_key)
    team_group = {}
    for group_table in data.get("standings", []):
        grp = group_table.get("group")
        if not grp:
            continue
        for row in group_table.get("table", []):
            team_group[row["team"]["id"]] = grp
    return team_group


def fetch_matches(api_key):
    data = api_get(f"competitions/{COMPETITION_CODE}/matches", {"season": SEASON}, api_key)
    return data.get("matches", [])


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
    lines.append(f"(Live snapshot as of {as_of_str} \u2014 reflects results up to this match's kickoff)")
    return "\n".join(lines)


def stable_uid(group, home, away):
    """Deterministic UID so re-running the script updates existing calendar
    events instead of creating duplicates for subscribers."""
    key = f"{group}-{home}-{away}".lower().replace(" ", "")
    h = hashlib.md5(key.encode()).hexdigest()[:16]
    return f"{h}@worldcup2026-group-stage"


def fmt_ics_dt(dt):
    return dt.strftime("%Y%m%dT%H%M%SZ")


def escape_ics_text(text):
    """Escape special characters per RFC 5545 section 3.3.11."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def build_calendar(matches, team_group, as_of_str):
    by_group = defaultdict(list)

    for match in matches:
        if match.get("stage") != "GROUP_STAGE":
            continue  # skip knockout rounds entirely

        home = match["homeTeam"]
        away = match["awayTeam"]

        # prefer the match's own group field; fall back to the standings-derived map
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
            if status in FINISHED_STATUSES and goals_home is not None and goals_away is not None
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

            # apply this matchday's results before computing the next matchday's snapshot
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
    as_of_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    print("Fetching group assignments from /standings...")
    team_group = fetch_team_to_group(api_key)
    if not team_group:
        sys.exit("ERROR: No standings data returned -- aborting without touching the existing file.")

    print("Fetching matches from /matches...")
    matches = fetch_matches(api_key)
    if not matches:
        sys.exit("ERROR: No matches returned -- aborting without touching the existing file.")

    print(f"Building calendar from {len(matches)} matches across {len(set(team_group.values()))} groups...")
    ics_text, event_count = build_calendar(matches, team_group, as_of_str)

    if event_count == 0:
        sys.exit("ERROR: Built 0 events -- something looks wrong upstream, aborting without writing.")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(ics_text)

    print(f"Wrote {event_count} events to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
