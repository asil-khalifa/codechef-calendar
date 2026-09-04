#!/usr/bin/env python3
"""Sync upcoming CodeChef contests into a Google Calendar.

Reads CodeChef's public contest API and upserts each matching contest as an
event in a dedicated calendar, using a Google service account. Safe to run
repeatedly -- event IDs are derived from the contest code, so re-runs update
in place rather than duplicating.

Commands:
    init      create the calendar and share it with the human owner
    sync      fetch contests and push them to the calendar
    list      show what would be synced, without touching the calendar
"""

import argparse
import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
KEY_PATH = BASE / "service-account.json"
LOG_PATH = BASE / "sync.log"

CONTESTS_URL = "https://www.codechef.com/api/list/contests/all"
CONTEST_URL_TEMPLATE = "https://www.codechef.com/{code}"
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Marks events this script owns, so pruning never touches anything else
# the user has put on the calendar by hand.
SOURCE_TAG = "codechef-sync"

log = logging.getLogger("codechef")


def setup_logging(verbose):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log.addHandler(stream)

    fileh = logging.FileHandler(LOG_PATH)
    fileh.setFormatter(fmt)
    log.addHandler(fileh)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def calendar_service():
    if not KEY_PATH.exists():
        sys.exit(
            f"Missing service account key at {KEY_PATH}\n"
            "Download the JSON key from Google Cloud and save it there."
        )
    creds = service_account.Credentials.from_service_account_file(
        str(KEY_PATH), scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# --------------------------------------------------------------------------
# CodeChef side
# --------------------------------------------------------------------------

def fetch_contests():
    """Return contests that are running now or scheduled in the future."""
    resp = requests.get(
        CONTESTS_URL,
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (codechef-calendar-sync)"},
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "success":
        raise RuntimeError(f"CodeChef API returned status={data.get('status')!r}")

    contests = list(data.get("present_contests") or [])
    contests += list(data.get("future_contests") or [])
    log.debug("API returned %d present + future contests", len(contests))
    return contests


def matches(name, include_patterns, deny_patterns):
    """Deny always wins over include."""
    for pat in deny_patterns:
        if re.search(pat, name, re.IGNORECASE):
            return False
    return any(re.search(pat, name, re.IGNORECASE) for pat in include_patterns)


def select_contests(contests, cfg):
    kept = []
    for c in contests:
        name = c.get("contest_name", "")
        if matches(name, cfg["include_patterns"], cfg["deny_patterns"]):
            kept.append(c)
        else:
            log.debug("skipping %s (%s)", c.get("contest_code"), name)
    return kept


def event_id_for(contest_code):
    """Deterministic, valid Calendar event ID.

    Google requires base32hex characters (0-9, a-v). A hex digest is a strict
    subset of that, so hashing the contest code is both safe and stable across
    runs -- which is what makes re-syncing idempotent.
    """
    digest = hashlib.sha1(contest_code.encode()).hexdigest()
    return f"cc{digest}"


def build_event(contest, cfg):
    code = contest["contest_code"]
    name = contest["contest_name"]
    url = CONTEST_URL_TEMPLATE.format(code=code)

    description = f"{name}\n\nContest code: {code}\n{url}"

    return {
        "id": event_id_for(code),
        "summary": f"CodeChef: {name}",
        "description": description,
        "location": url,
        "source": {"title": "CodeChef", "url": url},
        "start": {
            "dateTime": contest["contest_start_date_iso"],
            "timeZone": cfg["timezone"],
        },
        "end": {
            "dateTime": contest["contest_end_date_iso"],
            "timeZone": cfg["timezone"],
        },
        "reminders": {
            "useDefault": False,
            "overrides": cfg.get("reminders", []),
        },
        "extendedProperties": {
            "private": {"source": SOURCE_TAG, "contest_code": code},
        },
    }


# --------------------------------------------------------------------------
# Google Calendar side
# --------------------------------------------------------------------------

def cmd_init(cfg):
    """Create the calendar under the service account and hand ownership over."""
    svc = calendar_service()

    if cfg.get("calendar_id"):
        log.info("calendar_id already set (%s) -- nothing to do", cfg["calendar_id"])
        return

    created = svc.calendars().insert(
        body={
            "summary": cfg["calendar_name"],
            "description": "Upcoming CodeChef contests. Synced automatically from the Raspberry Pi.",
            "timeZone": cfg["timezone"],
        }
    ).execute()

    cal_id = created["id"]
    log.info("created calendar %s (%s)", cfg["calendar_name"], cal_id)

    owner = cfg["share_with"]
    svc.acl().insert(
        calendarId=cal_id,
        body={"role": "owner", "scope": {"type": "user", "value": owner}},
    ).execute()
    log.info("granted owner access to %s", owner)

    cfg["calendar_id"] = cal_id
    save_config(cfg)
    log.info("saved calendar_id to config.json")

    print("\nCalendar ready. Add it in Google Calendar with:")
    print("  Other calendars  ->  +  ->  Subscribe to calendar")
    print(f"  {cal_id}")


def existing_synced_events(svc, cal_id, time_min):
    """All future events on the calendar that this script created."""
    events = {}
    page_token = None
    while True:
        resp = svc.events().list(
            calendarId=cal_id,
            privateExtendedProperty=f"source={SOURCE_TAG}",
            timeMin=time_min,
            singleEvents=True,
            showDeleted=False,
            maxResults=250,
            pageToken=page_token,
        ).execute()
        for ev in resp.get("items", []):
            events[ev["id"]] = ev
        page_token = resp.get("nextPageToken")
        if not page_token:
            return events


def upsert(svc, cal_id, body, dry_run):
    """Create the event, or patch it if it already exists."""
    code = body["extendedProperties"]["private"]["contest_code"]

    if dry_run:
        log.info("[dry-run] would sync %s -- %s", code, body["summary"])
        return "dry-run"

    try:
        svc.events().insert(calendarId=cal_id, body=body).execute()
        log.info("created  %s  %s", code, body["summary"])
        return "created"
    except HttpError as e:
        if e.resp.status != 409:
            raise

    # 409 means the event ID already exists -- update it so renames, time
    # changes and un-cancellations all get picked up.
    svc.events().update(calendarId=cal_id, eventId=body["id"], body=body).execute()
    log.info("updated  %s  %s", code, body["summary"])
    return "updated"


def cmd_sync(cfg, dry_run):
    cal_id = cfg.get("calendar_id")
    if not cal_id:
        sys.exit("config.json has no calendar_id -- run:  sync.py init")

    contests = fetch_contests()
    selected = select_contests(contests, cfg)
    log.info("%d of %d contests matched the filters", len(selected), len(contests))

    for c in selected:
        log.debug("  %s  %s", c["contest_code"], c["contest_name"])

    svc = calendar_service()

    stats = {"created": 0, "updated": 0, "dry-run": 0}
    wanted_ids = set()
    for contest in selected:
        body = build_event(contest, cfg)
        wanted_ids.add(body["id"])
        stats[upsert(svc, cal_id, body, dry_run)] += 1

    removed = prune(svc, cal_id, wanted_ids, contests, selected, dry_run)

    log.info(
        "done: %d created, %d updated, %d removed",
        stats["created"], stats["updated"], removed,
    )


def prune(svc, cal_id, wanted_ids, all_contests, selected, dry_run):
    """Delete future events for contests CodeChef no longer lists.

    Guarded: if the API returned nothing at all we treat that as an outage
    rather than a mass cancellation, and leave the calendar alone.
    """
    if not all_contests:
        log.warning("API returned no contests at all -- skipping prune to be safe")
        return 0
    if not selected:
        log.warning("no contests matched filters -- skipping prune to be safe")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    existing = existing_synced_events(svc, cal_id, now)

    removed = 0
    for ev_id, ev in existing.items():
        if ev_id in wanted_ids:
            continue
        code = ev.get("extendedProperties", {}).get("private", {}).get("contest_code", "?")
        if dry_run:
            log.info("[dry-run] would remove %s -- %s", code, ev.get("summary"))
        else:
            svc.events().delete(calendarId=cal_id, eventId=ev_id).execute()
            log.info("removed  %s  %s", code, ev.get("summary"))
        removed += 1
    return removed


def cmd_list(cfg):
    contests = fetch_contests()
    selected = select_contests(contests, cfg)
    selected_codes = {c["contest_code"] for c in selected}

    print(f"\nCodeChef currently lists {len(contests)} upcoming contests:\n")
    for c in sorted(contests, key=lambda x: x["contest_start_date_iso"]):
        mark = "KEEP " if c["contest_code"] in selected_codes else "skip "
        print(
            f"  {mark} {c['contest_code']:<16} "
            f"{c['contest_start_date_iso'][:16].replace('T', ' ')}  "
            f"{c['contest_name']}"
        )
    print(f"\n{len(selected)} would be synced to the calendar.\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "sync", "list"])
    parser.add_argument("--dry-run", action="store_true",
                        help="show what sync would change, without writing")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    cfg = load_config()

    try:
        if args.command == "init":
            cmd_init(cfg)
        elif args.command == "sync":
            cmd_sync(cfg, args.dry_run)
        else:
            cmd_list(cfg)
    except Exception as e:
        log.error("%s: %s", type(e).__name__, e)
        raise


if __name__ == "__main__":
    main()
