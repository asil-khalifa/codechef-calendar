#!/bin/bash
# Regenerate the public .ics feeds and push them, from the Pi.
#
# GitHub's scheduled workflows are best-effort: they get delayed under load and
# are sometimes dropped entirely. Two consecutive hourly slots were missed on
# the day this was written. So the Pi -- whose cron does fire reliably -- is the
# primary publisher, and the Actions workflow is the backup for when the Pi is
# off or off the network.
#
# A side benefit: regular commits from here keep the repository active, so
# GitHub never hits its 60-day rule that disables scheduled workflows.

set -uo pipefail

REPO=/home/asil/codechef-calendar
cd "$REPO" || exit 1

LOG="$REPO/publish.log"
[ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 1048576 ] && mv -f "$LOG" "$LOG.1"

log() { echo "$(date -Is)  $*" >> "$LOG"; }

export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"

# Start from whatever is on the remote, so this never diverges from what the
# Actions run may have published. docs/ is regenerated below regardless.
git checkout -q -- docs 2>/dev/null
if ! git fetch -q origin main 2>>"$LOG"; then
    log "ERROR: fetch failed (network?), giving up this run"
    exit 1
fi
if ! git rebase -q origin/main 2>>"$LOG"; then
    git rebase --abort 2>/dev/null
    log "WARN: rebase failed, resetting to origin/main"
    git reset -q --hard origin/main
fi

# Refuse to publish on failure -- make_ics.py exits non-zero rather than
# writing an empty calendar when upstream is broken.
if ! ./venv/bin/python make_ics.py --out docs >>"$LOG" 2>&1; then
    log "ERROR: feed generation failed, nothing published"
    exit 1
fi

# Health of the third-party feeds is informational here; a dead Codeforces
# calendar must not stop us publishing our own.
./venv/bin/python check_feeds.py --out docs >>"$LOG" 2>&1

if git diff --quiet -- docs; then
    log "no change"
    exit 0
fi

git add docs
git commit -q -m "Update contest feeds" 2>>"$LOG"

if git push -q origin main 2>>"$LOG"; then
    log "published: $(git log -1 --format=%h)"
else
    log "ERROR: push failed; commit is local and will retry next run"
    exit 1
fi
