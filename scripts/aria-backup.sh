#!/usr/bin/env bash
#
# ARIA backup — dump the aria MongoDB database (via the running mongod
# container, since mongodump isn't on the host) plus the file-based identity
# state (SOUL/journals/skills), with rotation.
#
# Run on demand:   bash scripts/aria-backup.sh
# Automated:       installed as the aria-backup.timer systemd user unit (daily).
#
# Env:
#   ARIA_BACKUP_DIR   destination root (default ~/.aria/backups)
#   ARIA_BACKUP_KEEP  how many backups to retain (default 14)
#   ARIA_MONGO_CONTAINER  mongod container name (default shared-mongod)
set -euo pipefail

BACKUP_ROOT="${ARIA_BACKUP_DIR:-$HOME/.aria/backups}"
KEEP="${ARIA_BACKUP_KEEP:-14}"
CONTAINER="${ARIA_MONGO_CONTAINER:-shared-mongod}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$STAMP"
mkdir -p "$DEST"

# 1. MongoDB — stream a gzipped archive of the aria DB from the container to the host.
docker exec "$CONTAINER" mongodump \
  --uri="mongodb://localhost:27017/?directConnection=true&replicaSet=rs0" \
  --db=aria --gzip --archive > "$DEST/aria.archive.gz"

# 2. File-based identity + journals + skills.
for f in "$HOME/.aria/SOUL.md" "$HOME/.aria/HEARTBEAT.md"; do
  [ -f "$f" ] && cp "$f" "$DEST/" || true
done
for d in "$HOME/.aria/journals" "$HOME/.aria/skills"; do
  [ -d "$d" ] && cp -r "$d" "$DEST/" || true
done

# 3. Rotation — keep the newest $KEEP backups.
if [ -d "$BACKUP_ROOT" ]; then
  ls -1dt "$BACKUP_ROOT"/*/ 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -rf
fi

SIZE="$(du -sh "$DEST" | cut -f1)"
echo "ARIA backup complete: $DEST ($SIZE)"
echo "Restore with: docker exec -i $CONTAINER mongorestore --gzip --archive --drop < $DEST/aria.archive.gz"
