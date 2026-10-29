# JoanPi home

The homepage for the services on joan-pi, a late-90s portal page with a link to each service on the tailnet, plus two companion pages:

- **Pi Recipes** (`/recipes.html`): a command reference for the Pi.
- **Pi Chart** (`/chart/`): the Pi's health, live and over the last 48 hours.

```
site/                  homepage, Pi Recipes, icons, fonts (served by nginx as-is)
nginx/                 nginx config: serves site/, forwards /chart/ to Pi Chart
pichart/               Pi Chart: collector + its page (Python, standard library only)
  services.json        which containers are which service, and how to count their activity
compose.yaml           two containers: home (nginx) and pichart
```

Baby Buddy's and AdGuard Home's icons are not copied here: the homepage loads them from those instances, and shows the app's initials if that fails.

## Deploy

1. On the Pi: `git clone https://github.com/vronizor/joanpi-home ~/joanpi-home && cd ~/joanpi-home && docker compose up -d --build`. nginx listens on `127.0.0.1:8030` and Pi Chart on `127.0.0.1:8040`; set `HOME_PORT` or `PICHART_PORT` in `.env` to change them.
2. Give it a name on the tailnet with Tailscale Services: define a service `home` with port `tcp:443` in the admin console, run `sudo tailscale serve --service=svc:home --https=443 127.0.0.1:8030`, then approve the host on the Services page. Leave Funnel off. The homepage is then at `https://home.tailaccc17.ts.net` and Pi Chart at `/chart/`.
3. Optional, for AdGuard's DNS query counts in Pi Chart: put `ADGUARD_USERNAME=…` and `ADGUARD_PASSWORD=…` (AdGuard's web login) in `.env`, then `docker compose up -d`.

## Update

- Homepage or Pi Recipes: `git pull`. `site/` is mounted read-only into nginx, so nothing restarts.
- Pi Chart code: `git pull && docker compose up -d --build pichart`.
- `services.json` only: `docker compose restart pichart`.

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

Containers are matched to services by name or by compose project and service. A container that matches nothing still appears, under its own name; a service whose container isn't found shows as "Not found", which usually means `services.json` has the wrong name.

**Access:** Pi Chart runs with the host's network and process view, and mounts the Docker socket read-only. Read-only on the socket file doesn't limit what the API allows, so this container is effectively root on the Pi. It only answers GET requests, only on `127.0.0.1`, and is only reachable through the tailnet.

Tests: `cd pichart && python -m unittest tests/test_pichart.py`.

## Adding a service

Add a bubble in `.bubbles` and an entry in `.directory` in `site/index.html`, put its logo in `site/icons/`, add a line to `pichart/services.json`, and a row to the table in `site/recipes.html`.
