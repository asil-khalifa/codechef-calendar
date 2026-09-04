#!/bin/bash
# Cron entry point. Keeps the log from growing without bound and makes sure
# a transient network blip doesn't leave a half-day gap in the calendar.
set -uo pipefail

cd /home/asil/codechef-calendar || exit 1

# Rotate once the log passes ~1 MB.
if [ -f sync.log ] && [ "$(stat -c%s sync.log)" -gt 1048576 ]; then
    mv -f sync.log sync.log.1
fi

for attempt in 1 2 3; do
    if ./venv/bin/python sync.py sync; then
        exit 0
    fi
    echo "attempt $attempt failed, retrying in 60s" >> sync.log
    sleep 60
done

echo "$(date -Is)  ERROR: all 3 attempts failed" >> sync.log
exit 1
