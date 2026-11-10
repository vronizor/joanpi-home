#!/usr/bin/env bash
# Weekly: drop dangling images (left by every `up -d --build`) and build cache unused for a week.
set -euo pipefail
reclaimed() { { grep -E '^Total' || true; } | tail -n 1 | sed -E 's/^[^:]*:[[:space:]]*//'; }

# Capture then print: under systemd stderr is a journald socket, and `tee /dev/stderr` can't reopen it (ENXIO).
image_out="$(docker image prune -f)"
printf '%s\n' "$image_out"
cache_out="$(docker builder prune -f --filter until=168h)"
printf '%s\n' "$cache_out"
images="$(reclaimed <<<"$image_out")"
cache="$(reclaimed <<<"$cache_out")"
used="$(df --output=pcent / | tail -n 1 | tr -d ' ')"
echo "summary: images ${images:-0B}, build cache ${cache:-0B} freed; SD card ${used} used"
