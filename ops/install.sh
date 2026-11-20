#!/usr/bin/env bash
# Install or update the Pi's scheduled jobs (run from the repo checkout):
#   sudo ops/install.sh
# Re-run after every `git pull` that touches ops/.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root: sudo $0" >&2; exit 1; }
ops="$(cd "$(dirname "$0")" && pwd)"
etc=/etc/joanpi
lib=/usr/local/lib/joanpi

install -d -m 755 "$etc"
if [ ! -f "$etc/ops.env" ]; then
  owner="${SUDO_USER:-}"
  [ -n "$owner" ] && [ "$owner" != root ] || { echo "run with sudo from the owner's account" >&2; exit 1; }
  home="$(getent passwd "$owner" | cut -d: -f6)"
  sed -e "s|^OWNER=.*|OWNER=$owner|" -e "s|/home/you|$home|g" "$ops/ops.env.example" > "$etc/ops.env"
  chmod 644 "$etc/ops.env"
  echo "created $etc/ops.env for $owner"
fi
if [ ! -f "$etc/deploy.toml" ]; then
  install -m 644 "$ops/deploy.toml.example" "$etc/deploy.toml"
  echo "created $etc/deploy.toml"
fi
set -a; . "$etc/ops.env"; set +a

# Proton Drive: one CLI, one session, one backup user. Older setups kept these in
# Vélothèque's /etc/velotheque/backup.env; carry them over once.
old_velo_env=/etc/velotheque/backup.env
if [ ! -f "$etc/proton.env" ]; then
  if [ -f "$old_velo_env" ]; then
    { echo "# /etc/joanpi/proton.env — carried over from $old_velo_env by install.sh"
      grep -E '^(BACKUP_USER|PROTON_DRIVE_[A-Z_]+)=' "$old_velo_env"; } > "$etc/proton.env"
  else
    install -m 644 "$ops/proton.env.example" "$etc/proton.env"
  fi
  chmod 644 "$etc/proton.env"
  echo "created $etc/proton.env"
fi
BACKUP_USER="$(sed -n 's/^BACKUP_USER=//p' "$etc/proton.env")"; BACKUP_USER="${BACKUP_USER:-joanpibackup}"
if ! id "$BACKUP_USER" > /dev/null 2>&1; then
  useradd --system --home-dir /var/lib/joanpi-backup --create-home --shell /usr/sbin/nologin "$BACKUP_USER"
  echo "created system user $BACKUP_USER; log it in to Proton Drive next: joanpi-backup login (README, Scheduled jobs)"
fi
BACKUP_HOME="$(getent passwd "$BACKUP_USER" | cut -d: -f6)"

if [ ! -f "$etc/backup.toml" ]; then
  install -m 644 "$ops/backup.toml.example" "$etc/backup.toml"
  echo "created $etc/backup.toml"
fi
sed -i '\|^  "/etc/velotheque/backup.env",$|d' "$etc/backup.toml"
if [ -n "${VELOTHEQUE_DIR:-}" ] && [ -f "$VELOTHEQUE_DIR/compose.yaml" ] && ! grep -q '^name = "velotheque-db"' "$etc/backup.toml"; then
  data_dir="$(sed -n 's/^VELOTHEQUE_DATA_DIR=//p' "$VELOTHEQUE_DIR/.env" 2>/dev/null | tr -d "\"'")"
  [ -n "$data_dir" ] || data_dir="$(sed -n 's/^VELOTHEQUE_DATA_DIR=//p' "$old_velo_env" 2>/dev/null | tr -d "\"'")"
  [ -n "$data_dir" ] || data_dir="$VELOTHEQUE_DIR/data"
  if [ -d "$data_dir/photos" ]; then
    sed -e "s|@VELOTHEQUE_DIR@|$VELOTHEQUE_DIR|" -e "s|@PHOTOS_DIR@|$data_dir/photos|" \
      "$ops/backup-velotheque.toml.example" >> "$etc/backup.toml"
    echo "added Vélothèque (database, photos in $data_dir/photos) to $etc/backup.toml"
  else
    echo "Vélothèque not added to $etc/backup.toml: no $data_dir/photos (VELOTHEQUE_DATA_DIR in $VELOTHEQUE_DIR/.env)"
  fi
fi

# Vélothèque used to run its own Proton backup; joanpi-backup covers it now.
if [ -f /etc/systemd/system/velotheque-backup.timer ] || [ -d /usr/local/lib/velotheque-backup ]; then
  systemctl disable --now velotheque-backup.timer > /dev/null 2>&1 || true
  systemctl stop velotheque-backup.service > /dev/null 2>&1 || true
  rm -rf /etc/systemd/system/velotheque-backup.service /etc/systemd/system/velotheque-backup.timer \
    /etc/systemd/system/velotheque-backup.service.d /usr/local/lib/velotheque-backup \
    /var/lib/joanpi/jobs/velotheque-backup.json
  echo "removed Vélothèque's own backup job (now part of joanpi-backup)"
fi

install -d -m 755 "$lib" /var/lib/joanpi /var/lib/joanpi/jobs
install -m 755 "$ops"/lib/job_status.py "$ops"/lib/joanpi_backup.py "$ops"/lib/proton_drive.py "$ops"/lib/velo-sync.sh "$ops"/lib/docker-prune.sh "$ops"/lib/joanpi_deploy.py "$lib/"

timers=(joanpi-velo-sync joanpi-lux-backup joanpi-gym-pull joanpi-home-pull joanpi-docker-prune)
if sudo -u "$OWNER" docker compose up --help 2>/dev/null | grep -q -- --wait-timeout; then
  timers+=(joanpi-deploy)
  install -d -o "$OWNER" -g "$OWNER" -m 755 /var/lib/joanpi/deploy
  cat > /usr/local/bin/deploy <<EOF
#!/usr/bin/env bash
set -a; . /etc/joanpi/ops.env; set +a
if [ "\$(id -u)" = 0 ]; then exec sudo -u "$OWNER" -H OWNER_HOME="$OWNER_HOME" /usr/bin/python3 $lib/joanpi_deploy.py "\$@"; fi
exec /usr/bin/python3 $lib/joanpi_deploy.py "\$@"
EOF
  chmod 755 /usr/local/bin/deploy
else
  echo "skipped joanpi-deploy: needs Docker Compose 2.17 or newer (docker compose version)"
fi
timers+=(joanpi-backup)
if [ -d "$OWNER_HOME/pi-projects-data/.git" ]; then
  timers+=(joanpi-projects-pull)
else
  echo "skipped joanpi-projects-pull: clone the data repo to $OWNER_HOME/pi-projects-data first (README, Pi Projects)"
fi
for job in "${timers[@]}"; do
  for unit in "$job.service" "$job.timer"; do
    sed -e "s|@OWNER@|$OWNER|g" -e "s|@HOME@|$OWNER_HOME|g" \
        -e "s|@BACKUP_USER@|$BACKUP_USER|g" -e "s|@BACKUP_HOME@|$BACKUP_HOME|g" \
        "$ops/systemd/$unit" > "/etc/systemd/system/$unit"
  done
done

install -d -o "$BACKUP_USER" -g "$BACKUP_USER" -m 700 /var/lib/joanpi-backup
store="$(sed -n 's/^PROTON_DRIVE_CREDENTIALS_STORE=//p' "$etc/proton.env")"
bin="$(sed -n 's/^PROTON_DRIVE_BIN=//p' "$etc/proton.env")"
cat > /usr/local/bin/joanpi-backup <<EOF
#!/usr/bin/env bash
set -euo pipefail
case "\${1:-}" in
  now) start="\$(date +%s)"; sudo systemctl start joanpi-backup.service; journalctl -u joanpi-backup --since "@\$start" --no-pager -o cat ;;
  restore) shift; exec sudo -u "$BACKUP_USER" -H /usr/bin/python3 $lib/joanpi_backup.py restore "\$@" ;;
  login) exec sudo -u "$BACKUP_USER" -H env PROTON_DRIVE_CREDENTIALS_STORE=${store:-pass} ${bin:-proton-drive} auth login ;;
  *) echo "usage: joanpi-backup now | restore SOURCE [ARCHIVE] | login" >&2; exit 2 ;;
esac
EOF
chmod 755 /usr/local/bin/joanpi-backup

systemctl daemon-reload
for job in "${timers[@]}"; do systemctl enable --now "$job.timer" > /dev/null; done
systemctl list-timers 'joanpi-*' --no-pager

if [ -x /usr/local/bin/deploy ]; then
  echo
  echo "Apps deployed from tags ($etc/deploy.toml):"
  sudo -u "$OWNER" OWNER_HOME="$OWNER_HOME" python3 - "$etc/deploy.toml" <<'PY'
import os, sys, tomllib
from pathlib import Path
home = Path(os.environ["OWNER_HOME"])
for name, conf in tomllib.loads(Path(sys.argv[1]).read_text()).get("repos", {}).items():
    d = home / conf["dir"]
    print(f"  {name:10} {d}  {'ok' if (d / '.git').is_dir() else 'NOT FOUND: fix dir or remove the entry'}")
PY
fi

old="$(crontab -u "$OWNER" -l 2>/dev/null | grep -E 'gym && git pull|lux-archive/scripts/backup\.sh' || true)"
if [ -n "$old" ]; then
  echo
  echo "These crontab lines now run as timers too. Remove them with:"
  echo "  crontab -l | grep -vE 'gym && git pull|lux-archive/scripts/backup\\.sh' | crontab -"
  printf '  %s\n' "$old"
fi
