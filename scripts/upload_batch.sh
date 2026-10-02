#!/usr/bin/env bash
# Upload a folder of JPEGs as one weekly batch.
# Usage: scripts/upload_batch.sh <folder> [batch, default = ISO week e.g. 2026-W40]
set -euo pipefail
FOLDER="${1:?usage: upload_batch.sh <folder> [batch]}"
BATCH="${2:-$(date +%G-W%V)}"
STACK="${STACK:-ShutoStack}"
BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text)
# notes.txt first, so the AI sees it when the photos are processed
if [ -f "$FOLDER/notes.txt" ]; then
  aws s3 cp "$FOLDER/notes.txt" "s3://$BUCKET/incoming/$BATCH/notes.txt"
fi
echo "Uploading $FOLDER -> s3://$BUCKET/incoming/$BATCH/"
aws s3 sync "$FOLDER" "s3://$BUCKET/incoming/$BATCH/" \
  --exclude "*" --include "*.jpg" --include "*.jpeg" --include "*.JPG" --include "*.JPEG" --include "notes.txt"
