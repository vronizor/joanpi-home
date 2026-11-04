#!/usr/bin/env bash
# Weekly: drop dangling images (left by every `up -d --build`) and build cache unused for a week.
set -euo pipefail
reclaimed() { { grep -E '^Total' || true; } | tail -n 1 | sed -E 's/^[^:]*:[[:space:]]*//'; }

images="$(docker image prune -f | tee /dev/stderr | reclaimed)"
cache="$(docker builder prune -f --filter until=168h | tee /dev/stderr | reclaimed)"
used="$(df --output=pcent / | tail -n 1 | tr -d ' ')"
echo "summary: images ${images:-0B}, build cache ${cache:-0B} freed; SD card ${used} used"
