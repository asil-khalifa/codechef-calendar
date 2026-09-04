#!/usr/bin/env python3
"""Generate public iCalendar feeds of upcoming CodeChef contests.

Writes two files that anyone can subscribe to in Google Calendar, Apple
Calendar, Outlook or any RFC 5545 client:

    codechef-all.ics      every contest CodeChef lists
    codechef-rated.ics    Starters / Cook-Off / Lunchtime / Long Challenge
                          and anything marked (Rated)

Design notes that matter:

* Files are only rewritten when the *contest data* changes, so running this
  hourly in CI does not produce an hourly stream of empty commits.
* If the upstream API fails or returns nothing, this exits non-zero rather
  than writing an empty calendar. In CI that surfaces as a failed run and an
  email -- the whole point of this project is that dead feeds should not fail
  silently.
* Times are emitted in UTC. Every client converts correctly, and it avoids
  shipping a VTIMEZONE block that clients disagree about.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

CONTESTS_URL = "https://www.codechef.com/api/list/contests/all"
CONTEST_URL_TEMPLATE = "https://www.codechef.com/{code}"

PRODID = "-//asil-khalifa//codechef-calendar//EN"
UID_DOMAIN = "codechef-calendar.asilkhalifa.com"

RATED_INCLUDE = [
    r"\bstarters?\b",
    r"\bcook[- ]?off\b",
    r"\blunchtime\b",
    r"\blong challenge\b",
    r"\(\s*rated\s*\)",
]
RATED_DENY = [
    r"placement prep",
    r"\bpractice\b",
    r"\bmock\b",
]

FEEDS = {
    "codechef-all.ics": {
        "name": "CodeChef Contests (all)",
        "description": "Every contest CodeChef lists. Updated hourly.",
        "include": None,
        "deny": None,
    },
    "codechef-rated.ics": {
        "name": "CodeChef Contests (rated)",
        "description": (
            "Starters, Cook-Off, Lunchtime, Long Challenge and other rated "
            "contests. Updated hourly."
        ),
        "include": RATED_INCLUDE,
        "deny": RATED_DENY,
    },
}


# --------------------------------------------------------------------------
# RFC 5545 encoding
# --------------------------------------------------------------------------

def escape(text):
    """Escape a TEXT value. Order matters -- backslash has to go first."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line):
    """Fold a content line to 75 octets, per RFC 5545 section 3.1.

    Folding is measured in octets, not characters, so this walks the UTF-8
    encoding and never splits a multi-byte character across a fold.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line

    chunks, current, size = [], [], 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 75:
            chunks.append("".join(current))
            # Continuation lines carry a leading space, which counts toward
            # the 75-octet limit.
            current, size = [char], width + 1
        else:
            current.append(char)
            size += width
    chunks.append("".join(current))
    return "\r\n ".join(chunks)


def to_utc(iso_string):
    """'2026-09-09T20:00:00+05:30' -> '20260909T143000Z'"""
    dt = datetime.fromisoformat(iso_string)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without offset: {iso_string!r}")
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Feed building
# --------------------------------------------------------------------------

def fetch_contests():
    resp = requests.get(
        CONTESTS_URL,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (codechef-calendar; +https://github.com/asil-khalifa/codechef-calendar)"},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"CodeChef API status={data.get('status')!r}")

    contests = list(data.get("present_contests") or [])
    contests += list(data.get("future_contests") or [])
    return contests


def matches(name, include, deny):
    if include is None:
        return True
    if any(re.search(p, name, re.IGNORECASE) for p in deny or []):
        return False
    return any(re.search(p, name, re.IGNORECASE) for p in include)


def build_feed(contests, spec, stamp):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(spec['name'])}",
        f"X-WR-CALDESC:{escape(spec['description'])}",
        "X-WR-TIMEZONE:UTC",
        # Ask clients to re-poll hourly. Google largely ignores this, but
        # Apple and Thunderbird honour it.
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]

    kept = 0
    for contest in sorted(contests, key=lambda c: c["contest_start_date_iso"]):
        name = contest.get("contest_name", "")
        if not matches(name, spec["include"], spec["deny"]):
            continue
        kept += 1

        code = contest["contest_code"]
        url = CONTEST_URL_TEMPLATE.format(code=code)
        description = f"{name}\\n\\nContest code: {code}\\n{url}"

        lines += [
            "BEGIN:VEVENT",
            f"UID:{code}@{UID_DOMAIN}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{to_utc(contest['contest_start_date_iso'])}",
            f"DTEND:{to_utc(contest['contest_end_date_iso'])}",
            f"SUMMARY:{escape(name)}",
            f"DESCRIPTION:{description}",
            f"URL:{url}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n", kept


def volatile_stripped(text):
    """Feed content ignoring DTSTAMP, for change detection."""
    return "\n".join(
        l for l in text.splitlines() if not l.startswith("DTSTAMP:")
    )


def write_if_changed(path, content):
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if volatile_stripped(existing) == volatile_stripped(content):
            return False
    path.write_text(content, encoding="utf-8", newline="")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs", help="output directory")
    parser.add_argument("--check", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        contests = fetch_contests()
    except Exception as e:
        print(f"ERROR: could not fetch contests: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1

    # An empty list means the API shape changed or CodeChef is down. Never
    # overwrite good feeds with an empty one -- fail and let CI shout.
    if not contests:
        print("ERROR: API returned zero contests -- refusing to publish empty feeds",
              file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    changed_any = False

    for filename, spec in FEEDS.items():
        content, kept = build_feed(contests, spec, stamp)
        path = out / filename

        if args.check:
            differs = (not path.exists() or volatile_stripped(path.read_text(encoding="utf-8"))
                       != volatile_stripped(content))
            print(f"{filename}: {kept} events, {'CHANGED' if differs else 'unchanged'}")
            continue

        if write_if_changed(path, content):
            print(f"{filename}: {kept} events, written")
            changed_any = True
        else:
            print(f"{filename}: {kept} events, unchanged")

    if not args.check:
        # A small machine-readable summary for the website. Deliberately holds
        # no wall-clock time: a "last checked" field would change every run and
        # bury the repo in hourly no-op commits.
        upcoming = sorted(contests, key=lambda c: c["contest_start_date_iso"])
        status = {
            "upcoming_contests": len(upcoming),
            "next_contest": {
                "code": upcoming[0]["contest_code"],
                "name": upcoming[0]["contest_name"],
                "start_iso": upcoming[0]["contest_start_date_iso"],
            },
            "feeds": sorted(FEEDS),
        }
        body = json.dumps(status, indent=2) + "\n"
        if write_if_changed(out / "status.json", body):
            print(f"status.json: {len(upcoming)} upcoming contests, written")
            changed_any = True

        # Written on every run, unlike everything else here. It is deliberately
        # kept in its own file so the hourly churn stays out of the feeds'
        # history -- `git log -- docs/codechef-rated.ics` remains a genuine
        # changelog of when contests actually changed.
        (out / "checked.json").write_text(
            json.dumps({"checked_at": datetime.now(timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ")}, indent=2) + "\n"
        )
        print("checked.json: stamped")

    if changed_any:
        print("feeds updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
