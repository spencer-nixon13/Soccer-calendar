#!/usr/bin/env python3
"""Build an Apple/Google-compatible .ics calendar for a Let's Play Soccer team.

Reads the team's schedule from the same data the letsplaysoccer.com team page
uses, falls back to reading the team page itself, and writes the .ics file.
If neither source works, it exits with an error and leaves the old file alone,
so a site hiccup never wipes your calendar.

Settings come from environment variables (set in the GitHub workflow):
  TEAM_ID       team number from the team page URL (e.g. 494684)
  FACILITY_ID   facility number from the URL (16 = Lindon / Timpanogos)
  OUTPUT        output file name (e.g. soccer.ics)
  GAME_MINUTES  length of each calendar event (default 60)
  ALERT_MINUTES reminder before each upcoming game (default 60, 0 = none)
"""
import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TEAM_ID = os.environ.get("TEAM_ID", "494684")
FACILITY_ID = os.environ.get("FACILITY_ID", "16")
OUTPUT = os.environ.get("OUTPUT", "soccer.ics")
GAME_MINUTES = int(os.environ.get("GAME_MINUTES", "60"))
ALERT_MINUTES = int(os.environ.get("ALERT_MINUTES", "60"))
LOCAL_TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/Denver"))

API_URL = f"https://lps-api-prod.lps-test.com/teams/{TEAM_ID}"
PAGE_URL = f"https://www.letsplaysoccer.com/{FACILITY_ID}/teamSchedule/{TEAM_ID}?lang=en"
LOCATION = "Let's Play Sports - Timpanogos Indoor Soccer, Lindon, UT"
UA = {"User-Agent": "Mozilla/5.0 (team calendar feed)"}


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


# ---------- source 1: the JSON data behind the team page ----------
def games_from_api():
    data = json.loads(fetch(API_URL))
    team = data.get("team") or {}
    my_name = clean(team.get("team_name")) or None
    games = []
    for g in data.get("games") or []:
        home, away = g.get("home_team") or {}, g.get("visitor_team") or {}
        is_home = str(home.get("UTeamID")) == TEAM_ID
        me, them = (home, away) if is_home else (away, home)
        my_name = my_name or clean(me.get("team_name"))
        # The site stores local wall-clock time with a misleading "Z" suffix.
        start = datetime.strptime(g["SchedGameDateTime"][:16], "%Y-%m-%dT%H:%M")
        games.append({
            "id": str(g.get("UGameID")),
            "start": start.replace(tzinfo=LOCAL_TZ),
            "field": clean(g.get("field_name")),
            "home": is_home,
            "opponent": clean(them.get("team_name")),
            "my_color": clean(me.get("Color")).upper(),
            "their_color": clean(them.get("Color")).upper(),
            "result": clean(g.get("result")),
            "division": clean(team.get("division_name")),
            "season": team.get("Season"),
        })
    return my_name, games


# ---------- source 2: the team page HTML (backup) ----------
def games_from_page():
    page = fetch(PAGE_URL)
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    my_name = clean(html.unescape(title.group(1).split(" - ")[0])) if title else None
    rows = re.findall(r"<tr>(.*?)</tr>", page, re.S)
    games, today = [], datetime.now(LOCAL_TZ)
    for row in rows:
        cells = [clean(html.unescape(re.sub(r"<[^>]+>", " ", c)))
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 5:
            continue
        m = re.match(r"\w{3} (\d{1,2})/(\d{1,2}) (\d{1,2}):(\d{2}) ([ap]m)", cells[0], re.I)
        if not m:
            continue
        mo, d, h, mi, ap = int(m[1]), int(m[2]), int(m[3]), int(m[4]), m[5].lower()
        h = h % 12 + (12 if ap == "pm" else 0)
        # Page has no year: pick the year that puts the game closest to today.
        cands = [datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ) for y in (today.year - 1, today.year, today.year + 1)]
        start = min(cands, key=lambda c: abs(c - today))
        home_name, away_name = cells[2], cells[3]
        is_home = my_name is None or home_name == my_name
        games.append({
            "id": f"{start:%Y%m%d%H%M}",
            "start": start,
            "field": cells[1],
            "home": is_home,
            "opponent": away_name if is_home else home_name,
            "my_color": "", "their_color": "",
            "result": "" if cells[4] in ("-", "") else cells[4],
            "division": "", "season": None,
        })
    return my_name, games


# ---------- write the calendar ----------
def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    out, b = [], line.encode("utf-8")
    while len(b) > 75:
        cut = 75
        while (b[cut] & 0xC0) == 0x80:  # don't split a multi-byte character
            cut -= 1
        out.append(b[:cut].decode())
        b = b" " + b[cut:]
    out.append(b.decode())
    return "\r\n".join(out)


def nice(name):
    # "FC RATS" -> "FC Rats", "PRICE AMERICA" -> "Price America"
    return " ".join(w if len(w) <= 2 else w.capitalize() for w in name.split())


def build_ics(my_name, games):
    team_title = nice(my_name or "My Team")
    now = datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//soccer-calendar//letsplaysoccer//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(team_title)} Soccer",
        "X-WR-TIMEZONE:America/Denver",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H",
    ]
    for g in sorted(games, key=lambda g: g["start"]):
        s = g["start"].astimezone(timezone.utc)
        e = s + timedelta(minutes=GAME_MINUTES)
        side = "Home" if g["home"] else "Away"
        desc = [f"{side} game - {g['field']}"]
        if g["my_color"]:
            desc.append(f"Jerseys: us {g['my_color']}, them {g['their_color'] or '?'}")
        if g["result"]:
            desc.append(f"Final: {g['result']} (home - visitor)")
        if g["division"]:
            desc.append(f"{g['division']}, Season {g['season']}")
        desc.append(PAGE_URL)
        summary = f"⚽ {team_title} vs {nice(g['opponent'])}"
        if g["result"]:
            summary += f" ({g['result']})"
        lines += [
            "BEGIN:VEVENT",
            f"UID:lps-{TEAM_ID}-{g['id']}@letsplaysoccer-calendar",
            f"DTSTAMP:{now:%Y%m%dT%H%M%SZ}",
            f"DTSTART:{s:%Y%m%dT%H%M%SZ}",
            f"DTEND:{e:%Y%m%dT%H%M%SZ}",
            f"SUMMARY:{esc(summary)}",
            f"LOCATION:{esc(LOCATION + ' - ' + g['field'])}",
            f"DESCRIPTION:{esc(chr(10).join(desc))}",
            f"URL:{PAGE_URL}",
        ]
        if ALERT_MINUTES > 0 and not g["result"]:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(summary)}",
                      f"TRIGGER:-PT{ALERT_MINUTES}M", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def without_stamps(text):
    return re.sub(r"DTSTAMP:\S+", "", text)


def main():
    my_name, games = None, []
    for source in (games_from_api, games_from_page):
        try:
            my_name, games = source()
            if games:
                print(f"Got {len(games)} games from {source.__name__}")
                break
        except Exception as ex:  # try the next source
            print(f"{source.__name__} failed: {ex}", file=sys.stderr)
    if not games:
        sys.exit("No games found from either source - leaving the existing calendar unchanged.")

    new = build_ics(my_name, games)
    old = open(OUTPUT, encoding="utf-8", newline="").read() if os.path.exists(OUTPUT) else ""
    if without_stamps(old) == without_stamps(new):
        print("Schedule unchanged.")
        return
    with open(OUTPUT, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
