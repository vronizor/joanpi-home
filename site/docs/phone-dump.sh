#!/usr/bin/env bash
# Copy folders off an Android phone over adb, verify them by checksum, then delete only what was verified.
# Usage: phone-dump.sh map | copy|verify|delete <internal|sd> <folder> <dump-dir>
set -euo pipefail

INTERNAL=/storage/emulated/0
TMP_LIST=/data/local/tmp/phone-dump-delete.txt

die() { echo "phone-dump: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
phone-dump.sh map
    Biggest folders on internal storage and the SD card, and the apps using the most space.

phone-dump.sh copy   <internal|sd> <folder> <dump-dir>
phone-dump.sh verify <internal|sd> <folder> <dump-dir>
phone-dump.sh delete <internal|sd> <folder> <dump-dir>
    copy:   pull <folder> to <dump-dir>/<internal|sd>/<folder>, then verify.
    verify: compare checksums, phone against drive.
    delete: verify again, then delete from the phone only the files that are
            identical on the drive. Anything new or different stays on the phone.

    <folder> is relative to the volume root, e.g. DCIM/Camera.

Example:
    DUMP=/Volumes/MyDrive/phone-dump/2026-11-07
    phone-dump.sh copy internal DCIM/Camera "$DUMP"
    phone-dump.sh delete internal DCIM/Camera "$DUMP"
EOF
}

need_device() {
  command -v adb >/dev/null || die "adb not found (macOS: brew install android-platform-tools)"
  [ "$(adb get-state 2>/dev/null)" = device ] || die "no phone found: plug it in, enable USB debugging, accept the prompt"
}

sd_root() {
  local id
  id=$(adb shell ls /storage | tr -d '\r' | grep -E '^[0-9A-F]{4}-[0-9A-F]{4}$' | head -1)
  [ -n "$id" ] || die "no SD card found under /storage"
  echo "/storage/$id"
}

volume_root() {
  case "$1" in
    internal) echo "$INTERNAL" ;;
    sd) sd_root ;;
    *) die "volume must be 'internal' or 'sd', not '$1'" ;;
  esac
}

md5_local() {
  if command -v md5 >/dev/null; then
    find . -type f ! -name '.DS_Store' ! -name '._*' -exec md5 -r {} +
  else
    find . -type f ! -name '.DS_Store' ! -name '._*' -exec md5sum {} + | sed 's/  / /'
  fi
}

checksums() {
  local phone=$1 local_dir=$2 work=$3
  [ -d "$local_dir" ] || die "$local_dir doesn't exist: copy first"
  echo "Hashing on the phone…"
  adb shell "cd '$phone' && find . -type f -exec md5sum {} +" | tr -d '\r' | sed 's/  / /' | LC_ALL=C sort > "$work/phone"
  echo "Hashing on the drive…"
  (cd "$local_dir" && md5_local) | LC_ALL=C sort > "$work/local"
  LC_ALL=C comm -12 "$work/phone" "$work/local" > "$work/same"
  LC_ALL=C comm -23 "$work/phone" "$work/local" > "$work/phone_only"
}

report() {
  local work=$1 n_phone n_same n_missing
  n_phone=$(wc -l < "$work/phone" | tr -d ' ')
  n_same=$(wc -l < "$work/same" | tr -d ' ')
  n_missing=$(wc -l < "$work/phone_only" | tr -d ' ')
  echo "$n_same of $n_phone phone files are identical on the drive."
  if [ "$n_missing" -gt 0 ]; then
    echo "$n_missing missing or different on the drive (copy again to fix), e.g.:"
    head -5 "$work/phone_only" | cut -d' ' -f2- | sed 's/^/  /'
  fi
}

tree() {
  awk -F'\t' -v root="$1" '
    { size[$2] = $1; paths[n++] = $2 }
    function parent(p) { sub(/\/[^\/]*$/, "", p); return p }
    function depth(p) { return p == root ? 0 : split(substr(p, length(root) + 2), _, "/") }
    function key(p) { return p == root ? "" : key(parent(p)) "/" sprintf("%09d", 999999999 - size[p]) substr(p, length(parent(p)) + 2) }
    function shown(p,   q) {
      if (p == root) return 1
      q = parent(p)
      if (!shown(q) || size[p] < 100) return 0
      return depth(p) <= 1 || size[p] >= 0.25 * size[q]
    }
    END {
      for (i = 0; i < n; i++) {
        p = paths[i]
        if (!shown(p)) continue
        name = p == root ? root : substr(p, length(parent(p)) + 2)
        indent = ""
        for (d = 0; d < depth(p); d++) indent = indent "  "
        printf "%s\t%7d  %s%s\n", key(p), size[p], indent, name
      }
    }' | LC_ALL=C sort -t "$(printf '\t')" -k1,1 | cut -f2-
}

map() {
  local sd
  echo "== Internal storage (MB) =="
  adb shell "du -d6 -m $INTERNAL 2>/dev/null" | tr -d '\r' | tree "$INTERNAL"
  if sd=$(sd_root 2>/dev/null); then
    echo
    echo "== SD card (MB) =="
    adb shell "du -d6 -m $sd 2>/dev/null" | tr -d '\r' | tree "$sd"
  fi
  echo "(top-level folders of 100 MB or more; a subfolder shows when it's 100 MB or more and at least a quarter of its parent)"
  echo
  echo "== Apps by app + data size (GB) =="
  local work k
  work=$(mktemp -d)
  adb shell dumpsys diskstats | tr -d '\r' > "$work/ds"
  grep -E '^Data-Free' "$work/ds" || true
  for k in "Package Names" "App Sizes" "App Data Sizes" "Cache Sizes"; do
    grep "^$k:" "$work/ds" | sed -e "s/^$k: \[//" -e 's/\]$//' -e 's/"//g' | tr ',' '\n' > "$work/$k"
  done
  printf '%7s %7s %7s  %s\n' total data cache app
  paste "$work/Package Names" "$work/App Sizes" "$work/App Data Sizes" "$work/Cache Sizes" |
    awk -F'\t' '{ printf "%7.2f %7.2f %7.2f  %s\n", ($2 + $3) / 1e9, $3 / 1e9, $4 / 1e9, $1 }' |
    sort -rn | head -15
  echo "(an app's data includes its folder under Android/media, so don't add the two up)"
  rm -rf "$work"
}

main() {
  [ $# -ge 1 ] || { usage; exit 1; }
  local cmd=$1
  case "$cmd" in
    -h|--help|help) usage; exit 0 ;;
    map) need_device; map; exit 0 ;;
    copy|verify|delete) ;;
    *) usage; exit 1 ;;
  esac
  [ $# -eq 4 ] || { usage; exit 1; }
  local vol=$2 rel=${3%/} dump=${4%/}
  rel=${rel#/}
  [ -n "$rel" ] && [ "$rel" != . ] || die "give a folder, not the whole volume"
  need_device

  local root phone local_dir
  root=$(volume_root "$vol")
  phone="$root/$rel"
  local_dir="$dump/$vol/$rel"
  adb shell "[ -d '$phone' ]" || die "$phone not found on the phone"
  WORK=$(mktemp -d)
  trap 'rm -rf "$WORK"' EXIT

  if [ "$cmd" = copy ]; then
    mkdir -p "$(dirname "$local_dir")"
    echo "Copying $phone → $local_dir"
    adb pull -a "$phone" "$(dirname "$local_dir")/"
  fi

  checksums "$phone" "$local_dir" "$WORK"
  report "$WORK"

  if [ "$cmd" = delete ]; then
    local n
    n=$(wc -l < "$WORK/same" | tr -d ' ')
    [ "$n" -gt 0 ] || { echo "Nothing verified, nothing deleted."; exit 0; }
    echo "Before: $(adb shell "du -sm '$phone'" | tr -d '\r' | cut -f1) MB in $phone"
    read -r -p "Delete these $n verified files from the phone? [y/N] " answer
    [ "$answer" = y ] || [ "$answer" = Y ] || { echo "Nothing deleted."; exit 0; }
    cut -d' ' -f2- "$WORK/same" > "$WORK/list"
    adb push "$WORK/list" "$TMP_LIST" >/dev/null
    adb shell "cd '$phone' && while IFS= read -r f; do rm -f -- \"\$f\"; done < $TMP_LIST; rm -f $TMP_LIST"
    echo "After:  $(adb shell "du -sm '$phone'" | tr -d '\r' | cut -f1) MB in $phone"
    echo "Done. Reboot the phone so the gallery forgets the deleted files."
  fi
}

main "$@"
