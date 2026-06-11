#!/usr/bin/env bash
# Install the vixx.vn daily monitor as a cron job at 00:00 UTC.
# Run this ON the Linode, from inside the vixx-watch folder:  bash install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found on PATH"; exit 1; }

echo "vixx-watch dir : $DIR"
echo "python3        : $PY"

LINE="0 0 * * * cd $DIR && $PY $DIR/vixx_watch.py >> $DIR/data/cron.log 2>&1"

# Rebuild crontab: drop any old vixx_watch lines, ensure UTC, add ours.
TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -v 'vixx_watch.py' | grep -v '^CRON_TZ=' > "$TMP" || true
{ echo "CRON_TZ=UTC"; cat "$TMP"; echo "$LINE"; } | crontab -
rm -f "$TMP"

echo "Installed cron entry:"
echo "  $LINE"
echo
echo "Running one baseline crawl now..."
cd "$DIR" && "$PY" "$DIR/vixx_watch.py"
echo
echo "Done. Logs: $DIR/data/  (changelog.md, run.log, wayback.log, snapshots/)"
echo "Verify cron with:  crontab -l"
