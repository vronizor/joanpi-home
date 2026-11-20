# JoanPi home

The homepage for the services on joan-pi, a late-90s portal page with a link to each service on the tailnet and a search box that jumps straight to any of them, plus companion pages:

- **Pi Docs** (`/docs/`): reference pages written down once so they don't have to be worked out again, including Recipes, the Pi's command reference.
- **Pi Chart** (`/chart/`): the Pi's health, live and over the last 48 hours.
- **Pi Projects** (`/projects/`): personal projects scheduled against a monthly money budget and a weekly chore-time budget.

```
site/                  homepage, icons, fonts (served by nginx as-is)
  docs/                Pi Docs: index, pages, their shared docs.css/docs.js, and scripts they hand out
  projects/            Pi Projects page; its data/ comes from a private repo, never committed here
nginx/                 nginx config: serves site/, forwards /chart/ to Pi Chart, redirects the old /recipes.html
pichart/               Pi Chart: collector + its page (Python, standard library only)
  services.json        which containers are which service, and how to count their activity
compose.yaml           two containers: home (nginx) and pichart
```

Baby Buddy's and AdGuard Home's icons are not copied here: the homepage loads them from those instances, and shows the app's initials if that fails.

## Search

The box under the wordmark jumps to any link in the directory or the footer: type, then ↵ or Go!. It matches the link text, the service name, initials (`df`, `rc`), and the comma-separated `data-keys` on each link, so `gpx` finds *Import a route*. To make a page findable, add it to the directory with a `data-keys` attribute; there is no second list. When nothing matches, Go! does nothing. Picks are remembered per browser (`localStorage`, key `joanpi-search-picks`) and push that result up for the same query next time.

## Deploy

1. On the Pi: `git clone https://github.com/vronizor/joanpi-home ~/joanpi-home && cd ~/joanpi-home && docker compose up -d --build`. nginx listens on `127.0.0.1:8030` and Pi Chart on `127.0.0.1:8040`; set `HOME_PORT` or `PICHART_PORT` in `.env` to change them.
2. Give it a name on the tailnet with Tailscale Services: define a service `home` with port `tcp:443` in the admin console, run `sudo tailscale serve --service=svc:home --https=443 127.0.0.1:8030`, then approve the host on the Services page. Leave Funnel off. The homepage is then at `https://home.tailaccc17.ts.net` and Pi Chart at `/chart/`.
3. Optional, for AdGuard's DNS query counts in Pi Chart: put `ADGUARD_USERNAME=…` and `ADGUARD_PASSWORD=…` (AdGuard's web login) in `.env`, then `docker compose up -d`.

## Update

- Homepage, Pi Docs or Pi Projects: nothing to do once `joanpi-home-pull` is installed (it pulls every 15 minutes); otherwise `git pull`. `site/` is mounted read-only into nginx, so nothing restarts.
- Pi Chart code: `git pull && docker compose up -d --build pichart`.
- `services.json` only: `docker compose restart pichart`.
- Anything under `ops/`: `git pull && sudo ops/install.sh`.
- The apps themselves (Telex, Vélothèque, Kiosk…): push a `v*` tag; `joanpi-deploy` deploys it within 5 minutes. See `site/docs/deploy.html`.

## Pi Docs

Static pages in `site/docs/`, styled by `docs.css`; `docs.js` adds a Copy button to every `<div class="cmd"><pre>…</pre></div>`. The old `/recipes.html` address redirects to `/docs/recipes.html`.

| Page | What |
|---|---|
| `deploy.html` | Deploys: release tags, what the Pi does with them, what to do when one doesn't go live |
| `recipes.html` | Recipes: commands for joan-pi (connect, services and ports, deploy, Docker, health, jobs, troubleshooting) |
| `phone.html` | Android phone backup and cleanup from the Mac, with `phone-dump.sh` (map, copy, verify, delete only what verified) |

To add a page: copy `phone.html` for the header and footer, add it to the list in `docs/index.html`. A script a page hands out lives next to it and is fetched with `curl -fsSO https://home.tailaccc17.ts.net/docs/<script>`. This repo is public: nothing personal (IDs, prices, addresses beyond the tailnet names) goes in a page.

## Pi Projects

A static page: `site/projects/index.html` reads `data/projects.csv` and `data/settings.json` and plans the projects week by week in the browser.

The data is personal (budget, prices paid), so it lives in the private repo `vronizor/pi-projects-data`, cloned to `~/pi-projects-data` and mounted read-only at `site/projects/data/` (git-ignored here apart from a placeholder). nginx refuses any path with a dot segment, so the clone's `.git` isn't served. `joanpi-projects-pull` pulls it every 15 minutes, like `joanpi-home-pull` does for this repo.

Set it up once, on the Pi:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/projects_data -N "" -C joan-pi-projects-data
cat ~/.ssh/projects_data.pub    # add on GitHub: pi-projects-data → Settings → Deploy keys, read-only
cat >> ~/.ssh/config <<'CFG'

Host github-projects-data
  HostName github.com
  User git
  IdentityFile ~/.ssh/projects_data
  IdentitiesOnly yes
CFG
git clone git@github-projects-data:vronizor/pi-projects-data.git ~/pi-projects-data
cd ~/joanpi-home && git pull && sudo ops/install.sh && docker compose up -d home
```

Clone before `docker compose up`: if `~/pi-projects-data` doesn't exist, Docker creates it empty and owned by root, and the clone then fails. To keep the data elsewhere, set `PROJECTS_DATA_DIR` in `.env`.

`settings.json`: `rates` (a list of `{ "from": "YYYY-MM-DD", "eur_per_month": N }`; the rate in force on each 1st is what lands that month, so a raise is a new entry, not an edit), `chore_hours_per_week` and `hobby_hours_per_week` (two separate weekly budgets that don't carry over and don't cover each other), `max_active` (slots), `mode` (`skip` lets buys up to one month's budget jump the queue; `strict` keeps every purchase in rank order).

`ledger.csv` (`date,kind,amount_eur,note`) holds the money that isn't a project purchase:

- `balance`: what's actually in the pot that day. The ledger restarts from the latest one; anything dated on or before it is already included.
- `deposit`: extra money in (a gift, a sale, a month you saved more).
- `spend`: hobby money spent on something not in the list.

Savings today = latest balance + monthly rate on every 1st since + deposits − spending − projects bought since (`spent_eur`, or `cost_eur` until it's filled in, on `bought_on`; a bought project with no date counts as bought after the latest balance). Rows and rates dated in the future are counted by the plan when they land, so an expected bonus shows what it unlocks.

`projects.csv`, one row per project:

| Column | Meaning |
|---|---|
| `rank` | Priority, 1 first |
| `id`, `name` | Short id and display name |
| `status` | `todo`, `monitoring` (already watching listings), `bought` (paid, `work_h` is what's left), `done`, `dropped` |
| `list` | `project` (default): planned, takes a slot. `scheduled`: maintenance with a date, planned for money and hours but takes no slot. `box`: Pandora's box, one-day ideas, not planned at all |
| `cost_eur` | Estimated cost, rounded up with shipping and small extras |
| `spent_eur`, `bought_on` | What was actually paid and when, once bought; shown in the Ledger section, the price against the estimate |
| `buy_h`, `work_h` | Chore hours to buy, then to install or do the work |
| `hobby_h` | Hobby hours after buying, from the hobby budget |
| `monitor_weeks` | Weeks of watching listings before buying (calendar time, no hours) |
| `earliest`, `deadline` | Optional dates, `YYYY-MM-DD` |
| `notes` | Free text; quote it if it contains commas |

A slot means attention: watching, buying and working take one, saving up doesn't. A project with a deadline jumps the queue once the weeks it needs, plus four, would otherwise run out. The boxes on the page change the settings for a what-if only.

## Pi Chart

The collector samples every 5 seconds and the page refreshes every 5 seconds while it is open. The 48-hour history keeps a per-minute average and maximum in SQLite (`data/pichart.sqlite`, about one small row per minute); charts show 15-minute averages with the 15-minute peak as a band, because a 48-hour average hides the spikes that matter.

What it reads:

- CPU, memory, swap, load, uptime and network from `/proc`; temperature from `/sys`; free space from the filesystem `data/` lives on (the SD card).
- Undervoltage from the kernel's `rpi_volt` sensor, checked every second and kept per minute in the history. The Pi 5 has no "since boot" flag to read (the kernel driver clears it every 2 seconds), so the Power tile reports the last 48 hours instead. Thermal throttling shows as temperature.
- For a container that is down: why it stopped (out of memory, a Docker error, or its exit code), when, and the last error-looking line of its log (the last line if none looks like an error). The log is read once per exit, so a crash loop doesn't cost a log read every refresh. Those log lines show on the page, so anything a service prints on crash is visible to the tailnet.
- Per container: state, restarts and start time from the Docker API; CPU and memory from the container's cgroup files (cheaper than `docker stats`, which keeps a stream open per container). Where the memory controller is off, as is common on Raspberry Pi OS, memory falls back to the summed resident memory of the container's processes, which counts shared libraries once per process.
- Activity per service, set by `activity` in `services.json`:
  - `requests`: request lines counted from the container's log (uvicorn and nginx formats), `/health*` checks excluded. Pi Chart's own calls through nginx are not logged.
  - `traffic`: bytes in and out of the container's network.
  - `connections`: new TCP connections to the service's port, for a container on the host network, whose traffic can't be separated from the Pi's.
  - `dns`: AdGuard's own query and block counts (needs the credentials above).
- Whether each service answers: a TCP connection to its local port, so the check itself never shows up as a request.
- Docker disk use (images, volumes, build cache) and each container's log size, every 10 minutes.
- Apps waiting for a tag: `joanpi-deploy` notes, for each app in `deploy.toml`, whether the default branch has commits past the newest `v*` tag (one extra ref in the `ls-remote` it already runs; it fetches only when the branch or the tag moved). Pi Chart reads that from `/var/lib/joanpi/deploy/state.json` and lists them under *Waiting for a tag*.
- Scheduled jobs: the status files in `/var/lib/joanpi/jobs/` (see [Scheduled jobs](#scheduled-jobs)), read on each page refresh. The homepage shows a red line when one has failed or is over an hour late.

Containers are matched to services by name or by compose project and service. A container that matches nothing still appears, under its own name; a service whose container isn't found shows as "Not found", which usually means `services.json` has the wrong name.

**Access:** Pi Chart runs with the host's network and process view, and mounts the Docker socket read-only. Read-only on the socket file doesn't limit what the API allows, so this container is effectively root on the Pi. It only answers GET requests, only on `127.0.0.1`, and is only reachable through the tailnet.

Tests: `cd pichart && python -m unittest tests/test_pichart.py`; for `ops/`: `cd ops && python -m unittest discover tests`.

## Scheduled jobs

Everything the Pi does on a clock runs as a systemd timer, defined in `ops/`. Timers catch up on a run missed while the Pi was off (`Persistent=true`), log to the journal, and each run ends with `ops/lib/job_status.py` writing `/var/lib/joanpi/jobs/<job>.json` for Pi Chart's Jobs panel.

| Job | When | What |
|---|---|---|
| `joanpi-gym-pull` | every 15 min | `git pull` in the Tally checkout |
| `joanpi-home-pull` | every 15 min | `git pull` in this checkout, so homepage, Pi Docs and Pi Projects edits show up on their own. Changes under `ops/` or `pichart/` still need the steps in [Update](#update) |
| `joanpi-deploy` | every 5 min | deploys new `v*` release tags of the apps in `/etc/joanpi/deploy.toml`, with a check and a rollback (`ops/lib/joanpi_deploy.py`, see [Deploys](site/docs/deploy.html)) |
| `joanpi-velo-sync` | ~02:40 | Vélothèque's Strava sync (the "Sync now" job; a rate limit just resumes next night) |
| `joanpi-lux-backup` | 03:10 | Lux Archive's `scripts/backup.sh` (commits and pushes to its data repo) |
| `joanpi-backup` | ~04:15 | Proton Drive backup of every app's data: the sources in `/etc/joanpi/backup.toml`, Vélothèque's database and photos included |
| `joanpi-docker-prune` | Sun 05:00 | dangling images; build cache trimmed to the 2 GB most recently used |

Apps that schedule their own work (Telex pulls, Kiosk digests, Double Feature refreshes) keep showing it on their own pages.

### Install or update

```sh
cd ~/joanpi-home && git pull && sudo ops/install.sh
docker compose up -d --build pichart     # Jobs panel (mounts /var/lib/joanpi/jobs)
```

The first run creates `/etc/joanpi/ops.env` (owner, home folder, Vélothèque path), `/etc/joanpi/proton.env` and `/etc/joanpi/backup.toml` (with Vélothèque's database and photos when its checkout is there), enables the timers and prints them. If the old crontab lines for Tally or Lux are still there, it prints the command that removes them.

Run a job now: `sudo systemctl start joanpi-velo-sync`. Its log: `journalctl -u joanpi-velo-sync -n 50`.

### App data backup (`joanpi-backup`)

> **A backup isn't trusted until a restore has been tested.** Run the restore tests below after setup, and again from time to time.

One nightly job (about 04:15) for every app, with one Proton login on the Pi. Sources are listed in `/etc/joanpi/backup.toml`, in three kinds:

| Kind | For | What a run does |
|---|---|---|
| `files` (default) | small app folders, config | the paths become one dated `.tar.gz`; skipped when nothing changed and the last archive is still on the Drive |
| `postgres` | Vélothèque's database | `pg_dump --format=custom` in the Compose service, checked with `pg_restore --list`, then one dated `.tar.gz`. Tables listed in `exclude_table_data` keep their schema but not their rows (Vélothèque's GADM regions: they download again and would almost double the dump) |
| `mirror` | Vélothèque's photos | the folder copied file by file to `Backups/joanpi/<source>/`, only files new or changed since the last run, in batches of 200. Nothing is ever deleted on the Drive. An interrupted first upload resumes where it stopped |

1. **Stage** (root, the only step that can read every app's data and reach Docker): SQLite databases are copied with SQLite's backup API and checked with `PRAGMA integrity_check`, never copied as live files. `*.log`, `-wal`/`-shm`/`-journal`, `*.tmp`, `*.part` are skipped. A path that matches nothing fails the run, so a moved folder can't silently back up nothing. Mirror files are hard-linked into upload batches (copied if they're on another disk), never moved or changed.
2. **Upload** (the backup user, the only account holding the Proton session): archives go to `Backups/joanpi/<source>/<source>-<UTC date>T<time>Z.tar.gz`. Retention per archived source: the newest archive of each of the last 14 days and 8 ISO weeks; older ones go to the Proton Drive trash (`filesystem delete` only works on trashed items, so Proton or you empty the trash).

Deploy keys are left out on purpose: they're quicker to regenerate than to protect.

`joanpi-backup now` runs the whole backup and prints its log. `ops/lib/proton_drive.py` is the only code that knows the `proton-drive` CLI.

**Restore tests:**

```sh
joanpi-backup restore kiosk                # newest archive; or name one: kiosk-2026-11-05T0415Z.tar.gz
joanpi-backup restore velotheque-db        # prints the path of the .dump it unpacked
cd ~/velotheque && sudo scripts/restore-db.sh /var/lib/joanpi-backup/restore/velotheque-db/<archive>/velotheque-db.dump
#   → "RESTORE TEST OK: row counts match the live database", then: sudo scripts/restore-db.sh --drop-scratch
joanpi-backup restore velotheque-photos    # downloads the whole folder; slow, do it now and then
```

Archives unpack under `/var/lib/joanpi-backup/restore/<source>/` (file paths mirror the Pi's, from `/`), with SQLite's integrity check on every database. Nothing live is touched.

- **Put a source back:** `docker compose stop` the app, `sudo cp -a` the unpacked folder over its data folder, start it again.
- **Vélothèque's database:** `sudo scripts/restore-db.sh --live <dump>` in its checkout (stops the app, asks you to type the database name), then `docker compose exec app velotheque regions reload`.
- **Vélothèque's photos:** `sudo rsync -a /var/lib/joanpi-backup/restore/velotheque-photos/ "$VELOTHEQUE_DATA_DIR/photos/"`, then `docker compose exec app velotheque normalize` rebuilds thumbnails and photo rows.

**Proton setup** (`/etc/joanpi/proton.env`): the backup user, and the pinned CLI version. The CLI is pre-1.0, so the backup refuses to run unless `proton-drive --version` matches `PROTON_DRIVE_VERSION`; check a new release by hand, then change the pin. On the Pi this is still the user `velobackup` from when Vélothèque had its own backup: its home (`/var/lib/velotheque-backup`) holds the session, and renaming it would mean logging in again.

Setup from scratch (once):

```sh
# 1. The CLI: the linux/arm64 build from https://proton.me/drive/download, SHA-512 checked
sudo install -m 0755 proton-drive /usr/local/bin/proton-drive
# 2. proton.env, the backup user and the timers
sudo apt install pass && sudo ops/install.sh
# 3. A GPG key and password store for the backup user (no passphrase: the job runs unattended,
#    so file permissions protect it; its home is 700). USER is BACKUP_USER from proton.env
sudo -u USER -H gpg --batch --pinentry-mode loopback --passphrase '' --quick-gen-key joanpi-backup default default never
sudo -u USER -H pass init joanpi-backup
# 4. Log in: open the printed URL on the laptop and finish there
joanpi-backup login
```

**When the Proton session expires**, the night's run fails and Pi Chart shows it: `joanpi-backup login`, then `joanpi-backup now`. If `pass` is too fiddly, set `PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file` in `proton.env`, re-run `sudo ops/install.sh` and log in again: the session is then a plain JSON file in the backup user's home, kept `chmod 600` in a `700` folder.

What was checked live about `proton-drive` 0.9.0 (all undocumented): `filesystem list --json` returns nodes whose `name` is `{"ok": true, "value": "…"}`; errors exit 1 with a message on stderr; uploading a folder with `--folder-conflict-strategy merge` creates missing remote folders in one call; unchanged files are skipped on re-upload; per-command `--help` doesn't work.

### Docker log size (optional, once)

Docker keeps container logs forever by default. To cap them at 30 MB per container, create `/etc/docker/daemon.json` with:

```json
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
```

then `sudo systemctl restart docker` (this restarts every container) and recreate each stack with `docker compose up -d --force-recreate`, since the cap only applies to new containers. Pi Chart keeps working: it reads logs through the Docker API and counts rotated files in the log sizes.

## Adding a service

Add a bubble in `.bubbles` and an entry in `.directory` in `site/index.html`, put its logo in `site/icons/`, add a line to `pichart/services.json`, and a row to the table in `site/docs/recipes.html`.
