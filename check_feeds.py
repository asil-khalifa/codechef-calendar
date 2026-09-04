#!/usr/bin/env python3
"""Check whether public contest calendar feeds are still being updated.

This exists because the whole premise of the project is that calendar feeds
die quietly. A page that recommends feeds is only trustworthy if something
keeps checking that the recommendations are still alive -- so this runs in CI
next to the generator, and fails the build if a *recommended* feed goes stale.

Reads feeds.json, fetches each Google Calendar's public .ics anonymously, and
writes docs/feed-status.json with what it found.

Exit codes:
    0  all recommended feeds are live
    1  a recommended feed has no future events, or could not be fetched
"""

import argparse
import json
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
REGISTRY = BASE / "feeds.json"

ICAL_URL = "https://calendar.google.com/calendar/ical/{cid}/public/basic.ics"
SUBSCRIBE_URL = "https://calendar.google.com/calendar/r?cid={cid}"

# Fallback horizon when a feed does not declare its own. Per-feed values live
# in feeds.json because platforms schedule very differently -- see the
# _comment_horizon note there.
DEFAULT_HORIZON_DAYS = 14

DTSTART = re.compile(rb"^DTSTART[^:]*:(\d{8})", re.MULTILINE)


def fetch_feed(cid):
    """Return sorted event dates from a public Google Calendar, anonymously."""
    url = ICAL_URL.format(cid=urllib.parse.quote(cid, safe=""))
    resp = requests.get(url, timeout=45)
    resp.raise_for_status()

    body = resp.content
    dates = sorted({m.decode() for m in DTSTART.findall(body)})
    return dates, body.count(b"BEGIN:VEVENT")


def classify(dates, today, horizon_days):
    if not dates:
        return "empty", None

    latest = datetime.strptime(dates[-1], "%Y%m%d").date()
    if latest < today:
        return "dead", latest
    if latest < today + timedelta(days=horizon_days):
        return "stale", latest
    return "live", latest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if a recommended feed is not live")
    args = parser.parse_args()

    registry = json.loads(REGISTRY.read_text())
    today = datetime.now(timezone.utc).date()

    results, problems = [], []
    for feed in registry["feeds"]:
        cid = feed["google_calendar_id"]
        horizon = feed.get("min_horizon_days", DEFAULT_HORIZON_DAYS)
        try:
            dates, count = fetch_feed(cid)
            state, latest = classify(dates, today, horizon)
            error = None
        except Exception as e:
            dates, count, state, latest = [], 0, "error", None
            error = f"{type(e).__name__}: {e}"

        entry = {
            "key": feed["key"],
            "platform": feed["platform"],
            "name": feed["name"],
            "maintainer": feed["maintainer"],
            "recommended": feed["recommended"],
            "note": feed.get("note", ""),
            "state": state,
            "events": count,
            "first_event": dates[0] if dates else None,
            "last_event": dates[-1] if dates else None,
            "subscribe_url": SUBSCRIBE_URL.format(cid=urllib.parse.quote(cid, safe="")),
            "ics_url": ICAL_URL.format(cid=urllib.parse.quote(cid, safe="")),
        }
        if error:
            entry["error"] = error
        results.append(entry)

        flag = "OK  " if state == "live" else state.upper().ljust(4)
        last = latest.isoformat() if latest else "-"
        print(f"{flag} {feed['platform']:<11} {feed['name']:<32} "
              f"events={count:<5} last={last}")

        if feed["recommended"] and state != "live":
            problems.append(f"{feed['name']} is {state}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Sorted and wall-clock-free so the file only changes when a feed's
    # actual state changes -- no hourly no-op commits.
    payload = {"feeds": sorted(results, key=lambda r: (r["platform"], r["key"]))}
    (out / "feed-status.json").write_text(json.dumps(payload, indent=2) + "\n")

    if problems:
        print("\nRECOMMENDED FEED PROBLEM:", "; ".join(problems), file=sys.stderr)
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
