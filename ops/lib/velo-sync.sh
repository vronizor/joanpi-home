#!/usr/bin/env bash
# Nightly Strava sync for Vélothèque: the same job as its "Sync now" button.
set -uo pipefail
: "${VELOTHEQUE_DIR:?set VELOTHEQUE_DIR in /etc/joanpi/ops.env}"

out="$(docker compose --project-directory "$VELOTHEQUE_DIR" exec -T app velotheque sync 2>&1)"
rc=$?
printf '%s\n' "$out"

if grep -q "JobAlreadyRunning" <<<"$out"; then
  echo "summary: skipped, a sync was already running"
  exit 0
fi
[ "$rc" -eq 0 ] || { echo "summary: failed (exit $rc)"; exit "$rc"; }

line="$(grep -E '^job [0-9]+: ' <<<"$out" | tail -n 1)"
status="$(awk '{print $3}' <<<"$line")"
counts="$(sed -E "s/^job [0-9]+: [a-z_]+ ?//; s/[{}']//g" <<<"$line")"
case "$status" in
  succeeded) echo "summary: ${counts:-nothing new}" ;;
  rate_limited) echo "summary: Strava rate limit reached, the next run resumes (${counts})" ;;
  *) echo "summary: ${line:-no job line in the output}"; exit 1 ;;
esac
