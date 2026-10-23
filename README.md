# JoanPi home

The homepage for the services on joan-pi: a static late-90s portal page with a link to each service on the tailnet. It is one HTML file plus icons and two self-hosted fonts, served by nginx in a container.

```
site/
  index.html           the page
  favicon.svg          Jπ favicon; apple-touch-icon.png is the same at 180 px
  icons/               logos of the self-hosted apps (each app's repo holds the source)
  fonts/               Alfa Slab One and Merriweather Black Italic, OFL
```

Baby Buddy's and AdGuard Home's icons are not copied here: the page loads them from those instances. If one doesn't load, its bubble shows the app's initials instead.

## Deploy

1. On the Pi: `git clone https://github.com/vronizor/joanpi-home && cd joanpi-home && docker compose up -d`. It listens on `127.0.0.1:8030`; set `APP_PORT` in `.env` to change that.
2. Give it a name on the tailnet with Tailscale Services: define a service `home` with port `tcp:443` in the admin console, run `sudo tailscale serve --service=svc:home --https=443 127.0.0.1:8030`, then approve the host on the Services page. Leave Funnel off. The page is then at `https://home.tailaccc17.ts.net`.

## Update

`git pull`. The site folder is mounted read-only into the container, so there is no rebuild or restart.

## Adding a service

Add a bubble in `.bubbles` and an entry in `.directory` in `site/index.html`, and put its logo in `site/icons/` (the full logo for the 42 px bubble; a simplified one for the 24 px directory icon if the full one blurs at that size).
