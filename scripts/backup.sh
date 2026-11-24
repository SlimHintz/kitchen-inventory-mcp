#!/usr/bin/env bash
# Nightly SQLite backup via the .backup command (safe under WAL, unlike cp).
# Keeps the last 14 timestamped backups, deletes older ones.
#
# Cron line (runs nightly at 3:10am):
#   10 3 * * * KITCHEN_MCP_DB_PATH=/home/pi/kitchen-inventory-data/kitchen.db \
#     /home/pi/kitchen-inventory-mcp/scripts/backup.sh >> /home/pi/kitchen-inventory-data/backup.log 2>&1

set -euo pipefail

DB_PATH="${KITCHEN_MCP_DB_PATH:-$HOME/kitchen-inventory-data/kitchen.db}"
BACKUP_DIR="${KITCHEN_MCP_BACKUP_DIR:-$(dirname "$DB_PATH")/backups}"
KEEP=14

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB_PATH" ]; then
    echo "No database found at $DB_PATH, skipping backup." >&2
    exit 1
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
DEST="$BACKUP_DIR/kitchen_${TIMESTAMP}.db"

sqlite3 "$DB_PATH" ".backup '${DEST}'"
echo "Backed up $DB_PATH -> $DEST"

# Delete all but the 14 most recent backups.
ls -1t "$BACKUP_DIR"/kitchen_*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "Retained $(ls -1 "$BACKUP_DIR"/kitchen_*.db 2>/dev/null | wc -l | tr -d ' ') backups."
