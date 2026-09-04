# CodeChef contest calendar

A CodeChef contest calendar for Google Calendar, Apple Calendar and Outlook
that is **actually maintained**. Regenerated hourly from CodeChef's own API.

Every public CodeChef calendar feed I could find has been abandoned:

| Feed | Last event | Status |
|---|---|---|
| `jatin69/codechef-contest-calendar` — the most linked one | Jan 2021 | dead |
| CodeChef's own "CodeChef Events" | Dec 2025 | dead |
| CodeChef YouTube Events | Dec 2021 | dead |
| Daily Learning By CodeChef | — | empty |

Measured, not guessed: [`check_feeds.py`](check_feeds.py) fetches each one and
records what it finds in [`docs/feed-status.json`](docs/feed-status.json).

**Full write-up: <https://asilkhalifa.com/contest-calendars/>**

## Subscribe

No account, no setup — paste into Google Calendar under
*Other calendars → + → From URL*:

| Feed | URL |
|---|---|
| Rated contests only | `https://asilkhalifa.com/codechef-calendar/codechef-rated.ics` |
| Every contest | `https://asilkhalifa.com/codechef-calendar/codechef-all.ics` |

*Rated* covers Starters, Cook-Off, Lunchtime, Long Challenge and anything
marked `(Rated)`. *All* adds Placement Prep, practice and other filler.

Google re-polls external feeds on its own schedule, often 12–24h. If you want
contests to appear within minutes, use the sync below instead.

### Other platforms

These are not mine and I don't maintain them, but they work and this project
checks hourly that they still do:

| Platform | Maintainer | Calendar ID |
|---|---|---|
| Codeforces | [clist.by](https://clist.by) | `br1o1n70iqgrrbc875vcehacjg@group.calendar.google.com` |
| LeetCode | community | `tuppslu7fl1gbkcgsuohodopfle7euv4@import.calendar.google.com` |

## Push into your own calendar instead

For near-instant updates, custom reminders, and automatic handling of renamed
or cancelled contests, `sync.py` writes directly to a calendar you own via a
Google service account. Runs fine on a Raspberry Pi.

```bash
pip install -r requirements-sync.txt
cp config.example.json config.json

# Google Cloud Console: create a project, enable the Google Calendar API,
# create a service account, download a JSON key to service-account.json
python sync.py init          # creates the calendar and shares it with you
python sync.py sync          # push contests
```

| Command | Does |
|---|---|
| `sync.py list` | show what matches the filters, touching nothing |
| `sync.py sync --dry-run` | show what would change on the calendar |
| `sync.py sync` | push contests |
| `make_ics.py --out docs` | regenerate the public `.ics` feeds |
| `check_feeds.py --strict` | verify every recommended feed is still alive |

Schedule it hourly with `run-sync.sh`:

```cron
17 * * * * /path/to/codechef-calendar/run-sync.sh >/dev/null 2>&1
```

## How it avoids dying the same way

The failure mode this project exists to fix is *silence* — a scraper stops and
the calendar just quietly stops filling in. So:

- **Empty never overwrites good.** If the API fails or returns zero contests,
  the generator exits non-zero instead of publishing an empty calendar.
- **CI goes red, and GitHub emails.** That includes a recommended third-party
  feed going stale, so the advice on the site can't rot unnoticed.
- **Feeds are only rewritten when contest data changes**, so the commit history
  is a real changelog rather than hourly noise.
- **Stable UIDs** keyed on contest *code*, not name — CodeChef sometimes ships
  duplicate names (START257 briefly shipped as "Starters 255"), which would
  otherwise collapse two contests into one.

One caveat I can't fully engineer away: GitHub disables scheduled workflows
after 60 days without repository activity. If this repo ever goes quiet, that
is the first thing to check.

## Notes

- Times come from the API's `*_iso` fields and are emitted in UTC. The
  `contest_duration` field is unreliable for multi-day contests and is ignored.
- Feeds carry upcoming contests only, not history.
- Unofficial. Not affiliated with CodeChef, Codeforces or LeetCode.

MIT licensed.
