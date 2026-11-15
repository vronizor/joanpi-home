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
if [ ! -f "$etc/backup.toml" ]; then
  install -m 644 "$ops/backup.toml.example" "$etc/backup.toml"
  echo "created $etc/backup.toml"
fi
if [ ! -f "$etc/deploy.toml" ]; then
  install -m 644 "$ops/deploy.toml.example" "$etc/deploy.toml"
  echo "created $etc/deploy.toml"
fi
set -a; . "$etc/ops.env"; set +a

velo_env=/etc/velotheque/backup.env
backup=yes
if [ -f "$velo_env" ]; then
  BACKUP_USER="$(sed -n 's/^BACKUP_USER=//p' "$velo_env")"; BACKUP_USER="${BACKUP_USER:-velobackup}"
  VELO_STATE="$(sed -n 's/^STATE_DIR=//p' "$velo_env")"; VELO_STATE="${VELO_STATE:-/var/lib/velotheque-backup}"
  id "$BACKUP_USER" > /dev/null 2>&1 || backup=no
else
  backup=no
fi

install -d -m 755 "$lib" /var/lib/joanpi /var/lib/joanpi/jobs
install -m 755 "$ops"/lib/job_status.py "$ops"/lib/joanpi_backup.py "$ops"/lib/velo-sync.sh "$ops"/lib/docker-prune.sh "$ops"/lib/joanpi_deploy.py "$lib/"

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
[ "$backup" = yes ] && timers+=(joanpi-backup)
if [ -d "$OWNER_HOME/pi-projects-data/.git" ]; then
  timers+=(joanpi-projects-pull)
else
  echo "skipped joanpi-projects-pull: clone the data repo to $OWNER_HOME/pi-projects-data first (README, Pi Projects)"
fi
for job in "${timers[@]}"; do
  for unit in "$job.service" "$job.timer"; do
    sed -e "s|@OWNER@|$OWNER|g" -e "s|@HOME@|$OWNER_HOME|g" \
        -e "s|@BACKUP_USER@|${BACKUP_USER:-}|g" -e "s|@VELO_STATE@|${VELO_STATE:-}|g" \
        "$ops/systemd/$unit" > "/etc/systemd/system/$unit"
  done
done

if [ "$backup" = yes ]; then
  install -d -o "$BACKUP_USER" -g "$BACKUP_USER" -m 700 /var/lib/joanpi-backup
  install -d -m 755 /etc/systemd/system/velotheque-backup.service.d
  install -m 644 "$ops/systemd/velotheque-backup.service.d/joanpi-status.conf" /etc/systemd/system/velotheque-backup.service.d/
  cat > /usr/local/bin/joanpi-backup <<EOF
#!/usr/bin/env bash
set -euo pipefail
case "\${1:-}" in
  now) sudo systemctl start joanpi-backup.service; journalctl -u joanpi-backup -n 40 --no-pager -o cat ;;
  restore) shift; exec sudo -u "$BACKUP_USER" -H /usr/bin/python3 $lib/joanpi_backup.py restore "\$@" ;;
  *) echo "usage: joanpi-backup now | restore SOURCE [ARCHIVE]" >&2; exit 2 ;;
esac
EOF
  chmod 755 /usr/local/bin/joanpi-backup
else
  echo "skipped joanpi-backup: needs Vélothèque's Proton backup set up first ($velo_env and its user)"
fi

systemctl daemon-reload
for job in "${timers[@]}"; do systemctl enable --now "$job.timer" > /dev/null; done
systemctl list-timers 'joanpi-*' 'velotheque-*' --no-pager

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
