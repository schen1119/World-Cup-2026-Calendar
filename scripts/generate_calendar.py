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


LIVE_STATUSES = {"IN_PLAY", "PAUSED", "HALFTIME"}

# Fallback venue strings keyed on (home_name, away_name) as returned by the API.
# Values are "Stadium Name, City, Country" — split on first ", " to get the two
# display lines used in the schedule section.
# Keys are lowercase for case-insensitive matching.
VENUE_LOOKUP = {
    ("mexico", "south africa"):             "Estadio Azteca, Mexico City, Mexico",
    ("south korea", "czechia"):             "Estadio Akron, Guadalajara, Mexico",
    ("czechia", "south africa"):            "Mercedes-Benz Stadium, Atlanta, USA",
    ("mexico", "south korea"):              "Estadio Akron, Guadalajara, Mexico",
    ("czechia", "mexico"):                  "Estadio Azteca, Mexico City, Mexico",
    ("south africa", "south korea"):        "Estadio BBVA, Monterrey, Mexico",
    ("canada", "bosnia and herzegovina"):   "BMO Field, Toronto, Canada",
    ("qatar", "switzerland"):               "Levi's Stadium, Santa Clara, USA",
    ("switzerland", "bosnia and herzegovina"): "SoFi Stadium, Inglewood, USA",
    ("canada", "qatar"):                    "BC Place, Vancouver, Canada",
    ("switzerland", "canada"):              "BC Place, Vancouver, Canada",
    ("bosnia and herzegovina", "qatar"):    "Lumen Field, Seattle, USA",
    ("brazil", "morocco"):                  "MetLife Stadium, East Rutherford, USA",
    ("haiti", "scotland"):                  "Gillette Stadium, Foxborough, USA",
    ("scotland", "morocco"):                "Gillette Stadium, Foxborough, USA",
    ("brazil", "haiti"):                    "Lincoln Financial Field, Philadelphia, USA",
    ("scotland", "brazil"):                 "Hard Rock Stadium, Miami, USA",
    ("morocco", "haiti"):                   "Mercedes-Benz Stadium, Atlanta, USA",
    ("united states", "paraguay"):          "SoFi Stadium, Inglewood, USA",
    ("australia", "turkiye"):               "BC Place, Vancouver, Canada",
    ("australia", "türkiye"):               "BC Place, Vancouver, Canada",
    ("united states", "australia"):         "Lumen Field, Seattle, USA",
    ("turkiye", "paraguay"):                "Levi's Stadium, Santa Clara, USA",
    ("türkiye", "paraguay"):                "Levi's Stadium, Santa Clara, USA",
    ("turkiye", "united states"):           "SoFi Stadium, Inglewood, USA",
    ("türkiye", "united states"):           "SoFi Stadium, Inglewood, USA",
    ("paraguay", "australia"):              "Levi's Stadium, Santa Clara, USA",
    ("germany", "curacao"):                 "NRG Stadium, Houston, USA",
    ("ivory coast", "ecuador"):             "Lincoln Financial Field, Philadelphia, USA",
    ("côte d'ivoire", "ecuador"):           "Lincoln Financial Field, Philadelphia, USA",
    ("germany", "ivory coast"):             "BMO Field, Toronto, Canada",
    ("germany", "côte d'ivoire"):           "BMO Field, Toronto, Canada",
    ("ecuador", "curacao"):                 "Arrowhead Stadium, Kansas City, USA",
    ("curacao", "ivory coast"):             "Lincoln Financial Field, Philadelphia, USA",
    ("curacao", "côte d'ivoire"):           "Lincoln Financial Field, Philadelphia, USA",
    ("ecuador", "germany"):                 "MetLife Stadium, East Rutherford, USA",
    ("netherlands", "japan"):               "AT&T Stadium, Arlington, USA",
    ("sweden", "tunisia"):                  "Estadio BBVA, Monterrey, Mexico",
    ("netherlands", "sweden"):              "NRG Stadium, Houston, USA",
    ("tunisia", "japan"):                   "Estadio BBVA, Monterrey, Mexico",
    ("japan", "sweden"):                    "AT&T Stadium, Arlington, USA",
    ("tunisia", "netherlands"):             "Arrowhead Stadium, Kansas City, USA",
    ("belgium", "egypt"):                   "Lumen Field, Seattle, USA",
    ("iran", "new zealand"):                "SoFi Stadium, Inglewood, USA",
    ("belgium", "iran"):                    "SoFi Stadium, Inglewood, USA",
    ("new zealand", "egypt"):               "BC Place, Vancouver, Canada",
    ("egypt", "iran"):                      "Lumen Field, Seattle, USA",
    ("new zealand", "belgium"):             "BC Place, Vancouver, Canada",
    ("spain", "cape verde"):                "Mercedes-Benz Stadium, Atlanta, USA",
    ("saudi arabia", "uruguay"):            "Hard Rock Stadium, Miami, USA",
    ("spain", "saudi arabia"):              "Mercedes-Benz Stadium, Atlanta, USA",
    ("uruguay", "cape verde"):              "Hard Rock Stadium, Miami, USA",
    ("cape verde", "saudi arabia"):         "NRG Stadium, Houston, USA",
    ("uruguay", "spain"):                   "Estadio Akron, Guadalajara, Mexico",
    ("france", "senegal"):                  "MetLife Stadium, East Rutherford, USA",
    ("iraq", "norway"):                     "Gillette Stadium, Foxborough, USA",
    ("france", "iraq"):                     "Lincoln Financial Field, Philadelphia, USA",
    ("norway", "senegal"):                  "MetLife Stadium, East Rutherford, USA",
    ("norway", "france"):                   "Gillette Stadium, Foxborough, USA",
    ("senegal", "iraq"):                    "BMO Field, Toronto, Canada",
    ("argentina", "algeria"):               "Arrowhead Stadium, Kansas City, USA",
    ("austria", "jordan"):                  "Levi's Stadium, Santa Clara, USA",
    ("argentina", "austria"):               "AT&T Stadium, Arlington, USA",
    ("jordan", "algeria"):                  "Levi's Stadium, Santa Clara, USA",
    ("jordan", "argentina"):                "AT&T Stadium, Arlington, USA",
    ("algeria", "austria"):                 "Arrowhead Stadium, Kansas City, USA",
    ("portugal", "dr congo"):               "NRG Stadium, Houston, USA",
    ("portugal", "democratic republic of congo"): "NRG Stadium, Houston, USA",
    ("uzbekistan", "colombia"):             "Estadio Azteca, Mexico City, Mexico",
    ("portugal", "uzbekistan"):             "NRG Stadium, Houston, USA",
    ("colombia", "dr congo"):               "Estadio Akron, Guadalajara, Mexico",
    ("colombia", "democratic republic of congo"): "Estadio Akron, Guadalajara, Mexico",
    ("colombia", "portugal"):               "Hard Rock Stadium, Miami, USA",
    ("dr congo", "uzbekistan"):             "Mercedes-Benz Stadium, Atlanta, USA",
    ("democratic republic of congo", "uzbekistan"): "Mercedes-Benz Stadium, Atlanta, USA",
    ("england", "croatia"):                 "AT&T Stadium, Arlington, USA",
    ("ghana", "panama"):                    "BMO Field, Toronto, Canada",
    ("england", "ghana"):                   "Gillette Stadium, Foxborough, USA",
    ("panama", "croatia"):                  "BMO Field, Toronto, Canada",
    ("panama", "england"):                  "MetLife Stadium, East Rutherford, USA",
    ("croatia", "ghana"):                   "Lincoln Financial Field, Philadelphia, USA",
    # API name variants observed in live responses
    ("canada", "bosnia-herzegovina"):       "BMO Field, Toronto, Canada",
    ("switzerland", "bosnia-herzegovina"):  "SoFi Stadium, Inglewood, USA",
    ("bosnia-herzegovina", "qatar"):        "Lumen Field, Seattle, USA",
    ("australia", "turkey"):                "BC Place, Vancouver, Canada",
    ("turkey", "paraguay"):                 "Levi's Stadium, Santa Clara, USA",
    ("turkey", "united states"):            "SoFi Stadium, Inglewood, USA",
    ("paraguay", "turkey"):                 "Levi's Stadium, Santa Clara, USA",
    ("germany", "curaçao"):                 "NRG Stadium, Houston, USA",
    ("ecuador", "curaçao"):                 "Arrowhead Stadium, Kansas City, USA",
    ("curaçao", "ivory coast"):             "Lincoln Financial Field, Philadelphia, USA",
    ("curaçao", "côte d'ivoire"):           "Lincoln Financial Field, Philadelphia, USA",
    ("spain", "cape verde islands"):        "Mercedes-Benz Stadium, Atlanta, USA",
    ("uruguay", "cape verde islands"):      "Hard Rock Stadium, Miami, USA",
    ("cape verde islands", "saudi arabia"): "NRG Stadium, Houston, USA",
    ("portugal", "congo dr"):               "NRG Stadium, Houston, USA",
    ("colombia", "congo dr"):               "Estadio Akron, Guadalajara, Mexico",
    ("congo dr", "uzbekistan"):             "Mercedes-Benz Stadium, Atlanta, USA",
}


# Knockout stage venues keyed on UTC kickoff time (first 16 chars of utcDate,
# e.g. "2026-06-28T19:00"). Teams are TBD so team-name lookup won't work;
# kickoff slot is unique per match and known from the official FIFA schedule.
# All times converted from EDT (UTC-4). Venue names kept consistent with
# VENUE_LOOKUP above (Foxborough not Boston, Arlington not Dallas, etc.)
KNOCKOUT_VENUE_LOOKUP = {
    # Round of 32 — Jun 28–Jul 3
    "2026-06-28T19:00": "SoFi Stadium, Los Angeles, USA",
    "2026-06-29T17:00": "NRG Stadium, Houston, USA",
    "2026-06-29T20:30": "Gillette Stadium, Foxborough, USA",
    "2026-06-30T01:00": "Estadio BBVA, Monterrey, Mexico",
    "2026-06-30T17:00": "AT&T Stadium, Arlington, USA",
    "2026-06-30T21:00": "MetLife Stadium, East Rutherford, USA",
    "2026-07-01T01:00": "Estadio Azteca, Mexico City, Mexico",
    "2026-07-01T16:00": "Mercedes-Benz Stadium, Atlanta, USA",
    "2026-07-01T20:00": "Lumen Field, Seattle, USA",
    "2026-07-02T00:00": "Levi's Stadium, Santa Clara, USA",
    "2026-07-02T19:00": "SoFi Stadium, Los Angeles, USA",
    "2026-07-02T23:00": "BMO Field, Toronto, Canada",
    "2026-07-03T03:00": "BC Place, Vancouver, Canada",
    "2026-07-03T18:00": "AT&T Stadium, Arlington, USA",
    "2026-07-03T22:00": "Hard Rock Stadium, Miami, USA",
    "2026-07-04T01:30": "Arrowhead Stadium, Kansas City, USA",
    # Round of 16 — Jul 4–7
    "2026-07-04T17:00": "NRG Stadium, Houston, USA",
    "2026-07-04T21:00": "Lincoln Financial Field, Philadelphia, USA",
    "2026-07-05T20:00": "MetLife Stadium, East Rutherford, USA",
    "2026-07-06T00:00": "Estadio Azteca, Mexico City, Mexico",
    "2026-07-06T19:00": "AT&T Stadium, Arlington, USA",
    "2026-07-07T00:00": "Lumen Field, Seattle, USA",
    "2026-07-07T16:00": "Mercedes-Benz Stadium, Atlanta, USA",
    "2026-07-07T20:00": "BC Place, Vancouver, Canada",
    # Quarterfinals — Jul 9–11
    "2026-07-09T20:00": "Gillette Stadium, Foxborough, USA",
    "2026-07-10T19:00": "SoFi Stadium, Los Angeles, USA",
    "2026-07-11T21:00": "Hard Rock Stadium, Miami, USA",
    "2026-07-12T01:00": "Arrowhead Stadium, Kansas City, USA",
    # Semifinals — Jul 14–15
    "2026-07-14T19:00": "AT&T Stadium, Arlington, USA",
    "2026-07-15T19:00": "Mercedes-Benz Stadium, Atlanta, USA",
    # Third-place play-off — Jul 18
    "2026-07-18T21:00": "Hard Rock Stadium, Miami, USA",
    # Final — Jul 19
    "2026-07-19T19:00": "MetLife Stadium, East Rutherford, USA",
}


def resolve_venue(match):
    """Return the full venue string (Stadium, City, Country) for a match.
    Priority: (1) API field, (2) team-name lookup for group stage,
    (3) UTC-kickoff-time lookup for knockout stage."""
    venue = match.get("venue")
    if venue:
        return venue
    home = (match["homeTeam"]["name"] or "").lower()
    away = (match["awayTeam"]["name"] or "").lower()
    result = VENUE_LOOKUP.get((home, away)) or VENUE_LOOKUP.get((away, home))
    if result:
        return result
    # Knockout fallback: key on UTC kickoff datetime (first 16 chars of utcDate)
    utc_date = match.get("utcDate", "")
    if utc_date:
        key = utc_date[:16]   # e.g. "2026-06-28T19:00"
        return KNOCKOUT_VENUE_LOOKUP.get(key, "")
    return ""


def resolve_venue_parts(match):
    """Return (venue_name, city_country) for two-line display in the schedule.
    Splits 'Stadium Name, City, Country' on the first ', '."""
    full = resolve_venue(match)
    if not full:
        return ("TBD", "")
    parts = full.split(", ", 1)
    return (parts[0], parts[1]) if len(parts) > 1 else (parts[0], "")


# ── Stage labels ───────────────────────────────────────────────────────────────

STAGE_LABELS = {
    "LAST_32":       "Round of 32",
    "LAST_16":       "Round of 16",
    "QUARTER_FINALS":"Quarterfinal",
    "SEMI_FINALS":   "Semifinal",
    "THIRD_PLACE":   "3rd Place",
    "FINAL":         "Final",
}

# ── Bracket slot layout ────────────────────────────────────────────────────────
# Box: W=140, H=62  Slot height: 124  Gap between rounds: 24
# Column x positions:
#   Left  R32=2   R16=166  QF=330  SF=494  Final=658(w=160,h=80)
#   Right SF=842  QF=1006  R16=1170  R32=1334
# SVG viewBox: 1476 × 1150

BRACKET_SLOTS = [
    # Left R32 (x=2, w=140, h=62)
    ("2026-06-28T19:00",    2,  67, 140, 62, "left"),
    ("2026-06-29T17:00",    2, 191, 140, 62, "left"),
    ("2026-06-29T20:30",    2, 315, 140, 62, "left"),
    ("2026-06-30T01:00",    2, 439, 140, 62, "left"),
    ("2026-06-30T17:00",    2, 563, 140, 62, "left"),
    ("2026-06-30T21:00",    2, 687, 140, 62, "left"),   # MetLife
    ("2026-07-01T01:00",    2, 811, 140, 62, "left"),
    ("2026-07-01T16:00",    2, 935, 140, 62, "left"),
    # Left R16 (x=166)
    ("2026-07-04T17:00",  166, 129, 140, 62, "center"),
    ("2026-07-04T21:00",  166, 377, 140, 62, "center"),
    ("2026-07-05T20:00",  166, 625, 140, 62, "center"),  # MetLife
    ("2026-07-06T00:00",  166, 873, 140, 62, "center"),
    # Left QF (x=330)
    ("2026-07-09T20:00",  330, 253, 140, 62, "center"),
    ("2026-07-10T19:00",  330, 749, 140, 62, "center"),
    # Left SF (x=494)
    ("2026-07-14T19:00",  494, 501, 140, 62, "center"),
    # Final (x=658, wider & taller)
    ("2026-07-19T19:00",  658, 492, 160, 80, "final"),
    # Right SF (x=842)
    ("2026-07-15T19:00",  842, 501, 140, 62, "center"),
    # Right QF (x=1006)
    ("2026-07-11T21:00", 1006, 253, 140, 62, "center"),
    ("2026-07-12T01:00", 1006, 749, 140, 62, "center"),
    # Right R16 (x=1170)
    ("2026-07-06T19:00", 1170, 129, 140, 62, "center"),
    ("2026-07-07T00:00", 1170, 377, 140, 62, "center"),
    ("2026-07-07T16:00", 1170, 625, 140, 62, "center"),
    ("2026-07-07T20:00", 1170, 873, 140, 62, "center"),
    # Right R32 (x=1334)
    ("2026-07-01T20:00", 1334,  67, 140, 62, "left"),
    ("2026-07-02T00:00", 1334, 191, 140, 62, "left"),
    ("2026-07-02T19:00", 1334, 315, 140, 62, "left"),
    ("2026-07-02T23:00", 1334, 439, 140, 62, "left"),
    ("2026-07-03T03:00", 1334, 563, 140, 62, "left"),
    ("2026-07-03T18:00", 1334, 687, 140, 62, "left"),
    ("2026-07-03T22:00", 1334, 811, 140, 62, "left"),
    ("2026-07-04T01:30", 1334, 935, 140, 62, "left"),
    # 3rd Place (x=658, same width as Final)
    ("2026-07-18T21:00",  658, 1060, 160, 62, "third"),
]

# SVG connector paths — all coordinates derived from BRACKET_SLOTS above
_CONNECTORS = """\
<g stroke="#2a3d5c" stroke-width="1.75" fill="none">
  <path d="M142,98 H154 V222 M142,222 H154 M154,160 H166"/>
  <path d="M142,346 H154 V470 M142,470 H154 M154,408 H166"/>
  <path d="M142,594 H154 V718 M142,718 H154 M154,656 H166"/>
  <path d="M142,842 H154 V966 M142,966 H154 M154,904 H166"/>
  <path d="M306,160 H318 V408 M306,408 H318 M318,284 H330"/>
  <path d="M306,656 H318 V904 M306,904 H318 M318,780 H330"/>
  <path d="M470,284 H482 V780 M470,780 H482 M482,532 H494"/>
  <line x1="634" y1="532" x2="658" y2="532"/>
  <line x1="818" y1="532" x2="842" y2="532"/>
  <path d="M1006,284 H994 V780 M1006,780 H994 M994,532 H982"/>
  <path d="M1170,160 H1158 V408 M1170,408 H1158 M1158,284 H1146"/>
  <path d="M1170,656 H1158 V904 M1170,904 H1158 M1158,780 H1146"/>
  <path d="M1334,98 H1322 V222 M1334,222 H1322 M1322,160 H1310"/>
  <path d="M1334,346 H1322 V470 M1334,470 H1322 M1322,408 H1310"/>
  <path d="M1334,594 H1322 V718 M1334,718 H1322 M1322,656 H1310"/>
  <path d="M1334,842 H1322 V966 M1334,966 H1322 M1322,904 H1310"/>
  <line x1="564" y1="563" x2="564" y2="1058" stroke-dasharray="6,4"/>
  <line x1="912" y1="563" x2="912" y2="1058" stroke-dasharray="6,4"/>
  <line x1="564" y1="1058" x2="658" y2="1058"/>
  <line x1="912" y1="1058" x2="818" y2="1058"/>
</g>"""

_ROUND_LABELS = """\
<text x="72"   y="22" font-size="11" font-weight="700" fill="#60a5fa" text-anchor="middle" letter-spacing=".07em">ROUND OF 32</text>
<text x="236"  y="22" font-size="11" font-weight="700" fill="#60a5fa" text-anchor="middle" letter-spacing=".07em">ROUND OF 16</text>
<text x="400"  y="22" font-size="11" font-weight="700" fill="#60a5fa" text-anchor="middle" letter-spacing=".07em">QUARTERFINALS</text>
<text x="564"  y="22" font-size="11" font-weight="700" fill="#60a5fa" text-anchor="middle" letter-spacing=".07em">SEMIFINALS</text>
<text x="738"  y="22" font-size="11" font-weight="700" fill="#f5a623" text-anchor="middle" letter-spacing=".07em">FINAL</text>
<text x="912"  y="22" font-size="11" font-weight="700" fill="#60a5fa" text-anchor="middle" letter-spacing=".07em">SEMIFINALS</text>
<text x="1076" y="22" font-size="11" font-weight="700" fill="#60a5fa" text-anchor="middle" letter-spacing=".07em">QUARTERFINALS</text>
<text x="1240" y="22" font-size="11" font-weight="700" fill="#60a5fa" text-anchor="middle" letter-spacing=".07em">ROUND OF 16</text>
<text x="1404" y="22" font-size="11" font-weight="700" fill="#60a5fa" text-anchor="middle" letter-spacing=".07em">ROUND OF 32</text>"""


def _hx(s):
    """HTML-escape a string for safe embedding in SVG text."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_box(utc_key, x, y, w, h, layout, match):
    """
    Return SVG string for one bracket slot.
    match may be None if no API data found for this slot.
    """
    m = match or {}
    home_obj = (m.get("homeTeam") or {})
    away_obj = (m.get("awayTeam") or {})
    home = (home_obj.get("name") or "").strip()
    away = (away_obj.get("name") or "").strip()

    status = m.get("status", "")
    score_data = m.get("score") or {}
    ft   = score_data.get("fullTime") or {}
    cs   = score_data.get("currentScore") or score_data.get("halfTime") or {}
    winner   = score_data.get("winner", "")
    duration = score_data.get("duration", "REGULAR")

    if status in FINISHED_STATUSES:
        et_goals  = score_data.get("extraTime") or {}
        hg = (ft.get("home") or 0) + (et_goals.get("home") or 0)
        ag = (ft.get("away") or 0) + (et_goals.get("away") or 0)
        has_score = True
        is_live   = False
        suffix    = "" if duration == "REGULAR" else (" (ET)" if duration == "EXTRA_TIME" else " (PEN)")
    elif status in LIVE_STATUSES:
        hg = cs.get("home") if cs.get("home") is not None else 0
        ag = cs.get("away") if cs.get("away") is not None else 0
        has_score = True
        is_live   = True
        suffix    = ""
    else:
        hg = ag = None
        has_score = False
        is_live   = False
        suffix    = ""

    # Winner/loser colouring for finished matches
    if has_score and not is_live:
        hc = "#e8eaf0" if winner != "AWAY_TEAM"  else "#7a8099"
        ac = "#e8eaf0" if winner != "HOME_TEAM"  else "#7a8099"
        hw = 'font-weight="600"' if winner == "HOME_TEAM" else ""
        aw = 'font-weight="600"' if winner == "AWAY_TEAM" else ""
    else:
        hc = "#e8eaf0" if home else "#6b7280"
        ac = "#e8eaf0" if away else "#6b7280"
        hw = aw = ""

    hi = 'font-style="italic"' if not home else ""
    ai = 'font-style="italic"' if not away else ""
    hd = _hx(home) if home else "TBD"
    ad = _hx(away) if away else "TBD"

    # Venue + date
    venue_full = resolve_venue(m) if m else KNOCKOUT_VENUE_LOOKUP.get(utc_key, "")
    vname = venue_full.split(", ", 1)[0] if venue_full else ""
    is_ml = "metlife" in venue_full.lower()

    et_now = now_eastern()
    et_tz  = et_now.tzinfo
    utc_str = m.get("utcDate", "")
    if utc_str:
        try:
            utc_dt  = datetime.datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
            et_dt   = utc_dt.astimezone(et_tz)
            date_s  = et_dt.strftime("%b %-d")
            time_s  = et_dt.strftime("%I:%M %p ET").lstrip("0")
            meta    = f"{date_s} · {time_s}"
        except Exception:
            meta = vname
    else:
        meta = vname

    vfill  = "#f5a623" if is_ml else "#7a8099"
    vfw    = 'font-weight="600"' if is_ml else ""
    vpfx   = "★ " if is_ml else ""
    bstroke = "#f5a623" if layout == "final" else ("rgba(245,166,35,0.55)" if is_ml else "#1e2740")
    bsw     = "1.5"     if layout == "final" else ("1"                     if is_ml else ".75")

    p  = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="5" fill="#131929" stroke="{bstroke}" stroke-width="{bsw}"/>']
    cx = x + w // 2
    sx = x + w - 8   # score right-edge x

    def team_row(ty, tc, ti, tw_, td, score_val):
        row = [f'<text x="{x+10}" y="{ty}" font-size="14" fill="{tc}" {ti} {tw_}>{td}</text>']
        if score_val is not None:
            row.append(f'<text x="{sx}" y="{ty}" font-size="14" fill="{tc}" font-weight="700" text-anchor="end">{score_val}{suffix if score_val == hg else ""}</text>')
        return row

    if layout == "final":
        # 80px tall Final box: home / vs / away / venue
        if has_score:
            p.append(f'<text x="{cx-10}" y="{y+24}" font-size="13" fill="{hc}" {hi} text-anchor="end">{hd}</text>')
            p.append(f'<text x="{cx+10}" y="{y+24}" font-size="13" fill="{hc}" font-weight="700" text-anchor="start">{hg}</text>')
            p.append(f'<text x="{cx}"    y="{y+42}" font-size="10" fill="#7a8099" text-anchor="middle">vs</text>')
            p.append(f'<text x="{cx-10}" y="{y+60}" font-size="13" fill="{ac}" {ai} text-anchor="end">{ad}</text>')
            p.append(f'<text x="{cx+10}" y="{y+60}" font-size="13" fill="{ac}" font-weight="700" text-anchor="start">{ag}</text>')
        else:
            p.append(f'<text x="{cx}" y="{y+24}" font-size="13" fill="{hc}" {hi} text-anchor="middle">{hd}</text>')
            p.append(f'<text x="{cx}" y="{y+42}" font-size="10" fill="#7a8099" text-anchor="middle">vs</text>')
            p.append(f'<text x="{cx}" y="{y+60}" font-size="13" fill="{ac}" {ai} text-anchor="middle">{ad}</text>')
        p.append(f'<text x="{cx}" y="{y+74}" font-size="10" fill="{vfill}" {vfw} text-anchor="middle">{vpfx}{_hx(vname)}</text>')

    elif layout == "third":
        if home or away:
            p.append(f'<text x="{cx}" y="{y+24}" font-size="13" fill="{hc}" {hi} text-anchor="middle">{hd}</text>')
            p.append(f'<text x="{cx}" y="{y+42}" font-size="13" fill="{ac}" {ai} text-anchor="middle">{ad}</text>')
            p.append(f'<text x="{cx}" y="{y+57}" font-size="10" fill="{vfill}" {vfw} text-anchor="middle">{vpfx}{_hx(meta)}</text>')
        else:
            p.append(f'<text x="{cx}" y="{y+28}" font-size="13" font-style="italic" fill="#6b7280" text-anchor="middle">TBD vs TBD</text>')
            p.append(f'<text x="{cx}" y="{y+48}" font-size="10" fill="{vfill}" {vfw} text-anchor="middle">{vpfx}{_hx(meta)}</text>')

    elif layout == "center" and not (home or away):
        # Pure TBD centred
        p.append(f'<text x="{cx}" y="{y+28}" font-size="13" font-style="italic" fill="#6b7280" text-anchor="middle">TBD</text>')
        p.append(f'<text x="{cx}" y="{y+48}" font-size="10" fill="{vfill}" {vfw} text-anchor="middle">{vpfx}{_hx(meta)}</text>')

    else:
        # Left-aligned (R32 both sides, or center once teams known)
        if has_score:
            p += team_row(y+21, hc, hi, hw, hd, hg)
            p += team_row(y+40, ac, ai, aw, ad, ag)
        else:
            p.append(f'<text x="{x+10}" y="{y+21}" font-size="14" fill="{hc}" {hi}>{hd}</text>')
            p.append(f'<text x="{x+10}" y="{y+40}" font-size="14" fill="{ac}" {ai}>{ad}</text>')
        p.append(f'<text x="{x+10}" y="{y+56}" font-size="10.5" fill="{vfill}" {vfw}>{vpfx}{_hx(meta)}</text>')
        if is_live:
            p.append(f'<circle cx="{x+w-10}" cy="{y+10}" r="4" fill="#f87171"/>')

    return "\n".join(p)


def build_bracket_page(matches, as_of_str):
    """
    Generate docs/index.html — the live knockout-stage bracket.
    Teams, scores, and statuses update with each API refresh.
    """
    # Build lookup: utcDate[:16] → match object
    by_utc = {}
    for m in matches:
        key = (m.get("utcDate") or "")[:16]
        if key:
            by_utc[key] = m

    stage_counts = {}
    for m in matches:
        s = m.get("stage", "?")
        stage_counts[s] = stage_counts.get(s, 0) + 1
    print(f"  [HTML] Stage labels: {stage_counts}")

    # Render all bracket boxes
    boxes = []
    for (utc_key, x, y, w, h, layout) in BRACKET_SLOTS:
        match = by_utc.get(utc_key)
        boxes.append(render_box(utc_key, x, y, w, h, layout, match))

    # 3rd-place label (above the box)
    third_label = '<text x="738" y="1044" font-size="10" font-weight="700" fill="#7a8099" text-anchor="middle" letter-spacing=".07em">3RD PLACE</text>'

    boxes_svg = "\n".join(boxes)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>FIFA World Cup 2026 — Knockout Stage Bracket</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0a0f1e;
      color: #e8eaf0;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      min-height: 100vh;
      padding: 2rem 1rem 3rem;
    }}
    .page-header {{ text-align: center; margin-bottom: 1.75rem; }}
    .page-header h1 {{
      font-size: clamp(1.4rem, 3vw, 2rem);
      font-weight: 700;
      color: #fff;
      letter-spacing: .02em;
    }}
    .page-header h1 span {{ color: #f5a623; }}
    .page-header p {{ margin-top: .4rem; font-size: .85rem; color: #7a8099; }}
    .legend {{
      display: flex; gap: 1.5rem; justify-content: center;
      flex-wrap: wrap; margin-top: 1rem; font-size: .8rem; color: #7a8099;
    }}
    .legend span {{ display: flex; align-items: center; gap: 6px; }}
    .bracket-wrap {{ width: 100%; padding: .5rem 0 1rem; }}
    .bracket-wrap svg {{ display: block; width: 100%; height: auto; }}
    .footer-note {{
      text-align: center; margin-top: 1.5rem;
      font-size: .75rem; color: #4a5270;
      max-width: 700px; margin-left: auto; margin-right: auto;
    }}
  </style>
</head>
<body>
<div class="page-header">
  <h1>FIFA World Cup 2026 — <span>Knockout Stage</span></h1>
  <p>Last updated: {as_of_str} with latest available data from football-data.org</p>
  <div class="legend">
    <span><span style="color:#e8eaf0;font-weight:700">●</span> Confirmed / result</span>
    <span><span style="color:#7a8099">●</span> Eliminated / loser</span>
    <span><span style="color:#6b7280;font-style:italic">●</span> TBD</span>
    <span><span style="color:#f5a623">★</span> MetLife Stadium</span>
  </div>
</div>
<div class="bracket-wrap">
<svg viewBox="0 0 1476 1150"
     xmlns="http://www.w3.org/2000/svg"
     font-family="'Inter', -apple-system, BlinkMacSystemFont, sans-serif">
{_ROUND_LABELS}
{_CONNECTORS}
{third_label}
{boxes_svg}
</svg>
</div>
<p class="footer-note">
  Bracket connections between rounds are based on the official FIFA schedule.
  Teams, scores, and results update automatically with each refresh.
  &nbsp;·&nbsp; Subscribe to the full calendar (group + knockout):
  <a href="webcal://YOUR-USERNAME.github.io/YOUR-REPO/world-cup-2026-group-stage.ics"
     style="color:#60a5fa">calendar feed</a>
</p>
</body>
</html>
"""


def ko_uid(utc_key):
    """Stable UID for a knockout match event, keyed on its kickoff slot."""
    h_ = hashlib.md5(utc_key.encode()).hexdigest()[:16]
    return f"{h_}@worldcup2026-knockout"


def build_calendar(matches, team_group, as_of_str):
    """
    Build the full .ics feed covering:
      • All 72 group-stage matches (with progressive standings in descriptions)
      • All 32 knockout-stage matches (with round / venue / ET time in descriptions)
    """
    def is_group_stage(match):
        return "GROUP" in (match.get("stage") or "").upper()

    # ── Group stage ────────────────────────────────────────────────────────────
    by_group = defaultdict(list)
    for match in matches:
        if not is_group_stage(match):
            continue
        home    = match["homeTeam"]
        away    = match["awayTeam"]
        raw_grp = match.get("group") or team_group.get(home["id"]) or team_group.get(away["id"])
        if not raw_grp:
            continue
        letter  = group_letter(raw_grp)
        md      = match.get("matchday")
        kickoff = datetime.datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
        kickoff_utc = kickoff.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        venue   = resolve_venue(match) or "TBD"
        status  = match.get("status")
        full_t  = (match.get("score") or {}).get("fullTime") or {}
        gh, ga  = full_t.get("home"), full_t.get("away")
        score   = (gh, ga) if status in FINISHED_STATUSES and gh is not None else None
        by_group[letter].append({
            "matchday": md, "kickoff": kickoff_utc,
            "home": home["name"], "away": away["name"],
            "venue": venue, "score": score,
        })

    for letter in by_group:
        by_group[letter].sort(key=lambda m: (m["matchday"] or 0, m["kickoff"]))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//World Cup 2026 Auto-Updater//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:FIFA World Cup 2026 - Complete Schedule",
        "X-WR-CALDESC:Auto-updated calendar: all 72 group-stage + 32 knockout matches, refreshed via football-data.org.",
        "X-WR-TIMEZONE:UTC",
    ]

    now_stamp   = fmt_ics_dt(datetime.datetime.utcnow())
    event_count = 0

    for letter, glist in sorted(by_group.items()):
        stats = {}
        for m in glist:
            stats.setdefault(m["home"], new_team_stats())
            stats.setdefault(m["away"], new_team_stats())

        matchdays = sorted(set(m["matchday"] for m in glist if m["matchday"]))
        for md_num in matchdays:
            md_matches = [m for m in glist if m["matchday"] == md_num]
            md_label   = "before Matchday 1" if md_num == 1 else f"after Matchday {md_num - 1}"
            desc_std   = standings_block(letter, stats, md_label, as_of_str)

            for m in md_matches:
                end  = m["kickoff"] + datetime.timedelta(hours=2)
                uid  = stable_uid(letter, m["home"], m["away"])
                summ = f"Group {letter}: {m['home']} vs {m['away']}"
                desc = (
                    f"FIFA World Cup 2026 Group Stage - Group {letter}\n"
                    f"{m['home']} vs {m['away']}\n"
                    f"Venue: {m['venue']}\n\n"
                    f"{desc_std}"
                )
                lines += [
                    "BEGIN:VEVENT",
                    f"UID:{uid}",
                    f"DTSTAMP:{now_stamp}",
                    f"DTSTART:{fmt_ics_dt(m['kickoff'])}",
                    f"DTEND:{fmt_ics_dt(end)}",
                    f"SUMMARY:{escape_ics_text(summ)}",
                    f"LOCATION:{escape_ics_text(m['venue'])}",
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

    # ── Knockout stage ─────────────────────────────────────────────────────────
    et_tz = now_eastern().tzinfo
    for match in sorted(matches, key=lambda m: m.get("utcDate", "")):
        stage = (match.get("stage") or "").upper()
        if is_group_stage(match) or not stage:
            continue
        round_label = STAGE_LABELS.get(stage, stage.replace("_", " ").title())
        utc_key     = (match.get("utcDate") or "")[:16]
        venue_full  = resolve_venue(match) or KNOCKOUT_VENUE_LOOKUP.get(utc_key, "TBD")

        home_obj = match.get("homeTeam") or {}
        away_obj = match.get("awayTeam") or {}
        home_nm  = (home_obj.get("name") or "").strip() or "TBD"
        away_nm  = (away_obj.get("name") or "").strip() or "TBD"

        kickoff = datetime.datetime.fromisoformat(
            (match.get("utcDate") or "2026-01-01T00:00:00Z").replace("Z", "+00:00")
        )
        kickoff_utc = kickoff.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        kickoff_et  = kickoff.astimezone(et_tz)
        et_str      = kickoff_et.strftime("%-m/%-d at %-I:%M %p ET")

        # End: 2.5h to allow for extra time and penalties
        end = kickoff_utc + datetime.timedelta(hours=2, minutes=30)

        uid  = ko_uid(utc_key)
        summ = f"{round_label}: {home_nm} vs {away_nm}"
        desc = (
            f"FIFA World Cup 2026 — {round_label}\n"
            f"{home_nm} vs {away_nm}\n"
            f"Kickoff: {et_str}\n"
            f"Venue: {venue_full}"
        )
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_stamp}",
            f"DTSTART:{fmt_ics_dt(kickoff_utc)}",
            f"DTEND:{fmt_ics_dt(end)}",
            f"SUMMARY:{escape_ics_text(summ)}",
            f"LOCATION:{escape_ics_text(venue_full)}",
            f"DESCRIPTION:{escape_ics_text(desc)}",
            "END:VEVENT",
        ]
        event_count += 1

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n", event_count


def main():
    api_key   = get_api_key()
    as_of_str = as_of_string()

    print("Step 1: Finding the correct season and fetching matches...")
    season, matches = find_working_season(api_key)

    print("Step 2: Fetching group assignments...")
    team_group = fetch_team_to_group_from_standings(api_key, season)
    if not team_group:
        print("  Falling back to deriving group assignments from match data...")
        team_group = derive_team_to_group_from_matches(matches)
    if not team_group:
        sys.exit(
            "ERROR: Could not determine group assignments. "
            "The API may not be returning group information yet."
        )
    print(f"  Found {len(set(team_group.values()))} groups covering {len(team_group)} teams.")

    print("Step 3: Building calendar (.ics — group + knockout)...")
    ics_text, event_count = build_calendar(matches, team_group, as_of_str)
    if event_count == 0:
        sys.exit("ERROR: Built 0 ICS events — check that matches include group-stage data.")
    os.makedirs(os.path.dirname(OUTPUT_ICS), exist_ok=True)
    with open(OUTPUT_ICS, "w") as f:
        f.write(ics_text)
    print(f"  Wrote {event_count} events to {OUTPUT_ICS}")

    print("Step 4: Building bracket page (index.html)...")
    html = build_bracket_page(matches, as_of_str)
    with open(OUTPUT_HTML, "w") as f:
        f.write(html)
    print(f"  Wrote bracket page to {OUTPUT_HTML}")

    print(f"\nDone (season={season}, as of {as_of_str}).")


if __name__ == "__main__":
    main()
