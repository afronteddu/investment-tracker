#!/bin/bash
# Upload latest DeGiro transactions xlsx to Render — run daily at 7am via launchd.
# Picks the newest .xlsx in data/transactions/ automatically.

RENDER_URL="https://investment-tracker-dafs.onrender.com"
USER="andrea"
PASS="claude"
DATA_DIR="/Users/afronteddu/dev/investment-tracker/data/transactions"
LOG="/Users/afronteddu/dev/investment-tracker/logs/render-upload.log"

# Find newest xlsx
LATEST=$(ls -t "$DATA_DIR"/*.xlsx 2>/dev/null | head -1)

if [ -z "$LATEST" ]; then
  echo "[$(date)] No xlsx files found in $DATA_DIR" >> "$LOG"
  exit 1
fi

echo "[$(date)] Uploading $LATEST to Render..." >> "$LOG"

RESPONSE=$(curl -s -u "${USER}:${PASS}" \
  -F "file=@${LATEST}" \
  "${RENDER_URL}/api/upload")

echo "[$(date)] Response: $RESPONSE" >> "$LOG"

# Also ping the health endpoint to warm up the dyno
curl -s -u "${USER}:${PASS}" "${RENDER_URL}/api/portfolio" > /dev/null
echo "[$(date)] Dyno warmed up." >> "$LOG"
