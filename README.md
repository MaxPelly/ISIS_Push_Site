>  **Note:** This branch includes AI-assisted code generation. Treat it with appropriate caution.

# ISIS Push Site

Mobile push notifications for the ISIS Beam Monitor, as a small installable web app (PWA).

The monitor daemon POSTs each notification to this site as HMAC-signed JSON. The site keeps a short feed of them and sends Web Push messages to each device that wants that notification's topic. People join with an invite link, choose topics (TS1, TS2, Muons, MCR news, the daily summary, individual instruments) and optional quiet hours, and can read recent notifications in the app. The feed keeps the newest 75 of each kind (beam, MCR news, daily summary and experiment notifications) for up to 30 days, set by `[STORAGE] feed_items_per_kind` and `feed_max_days`. The monitor also posts its status within a few seconds of any change, which the app's Status section shows (see [The Status section](#the-status-section)).

It's a separate project, the monitor's optional companion, installed on its own. It never imports the monitor's code, and the signed JSON payloads (described below) are the only contract between them.

## How it fits together

```
monitor daemon ──signed POST──► /run/isis-push-site/ingest.sock ─┐
                                                                 ├─ push_site (aiohttp + SQLite)
phones ──HTTPS──► Caddy (TLS) ──► 127.0.0.1:8765 ────────────────┘        │
                                                                          └─► Web Push (Apple / Google / Mozilla) ──► phones
```

### Security model

- **Ingest:**
  - `/ingest` and `/ingest/status` are only for the monitor daemon on the same host, and only exist on the site's Unix socket (`[SERVER] ingest_socket`), which only the site's group, `isis-ingest`, can reach. On the public listener they're 404, and the reverse proxy refuses them too.
  - They then need a valid `X-Signature` (HMAC-SHA256 of `<X-Timestamp>.<body>` under the shared secret) and a timestamp within 60 seconds. A repeated notification id is dropped while it's still in the feed.
- **Accounts:**
  - There are none. An admin creates invite links on the server, and redeeming one gives that browser a random device token in a `__Host-` cookie (`Secure`, `HttpOnly`, `SameSite=Strict`). A device that goes unused for a year is signed out.
  - Only SHA-256 hashes of tokens are stored, and devices can be listed and revoked from the command line.
  - The invite token sits in the link's `#fragment`, so it never reaches server or proxy logs.
- **API:**
  - Every change (any `/api/` request but `GET` or `HEAD`) must be JSON with an `Origin` header equal to `public_origin`.
  - Push subscriptions are only accepted for the browsers' push services (FCM, Apple, Mozilla, Windows), with keys that are checked to be valid. This means the site can't be used to send requests to arbitrary servers.
- **Pages:**
  - The pages have a strict Content Security Policy (no inline script or style) and set all text with `textContent`.
  - `nosniff`, `Referrer-Policy: same-origin`, `frame-ancestors 'none'` and a `Permissions-Policy` turning off the camera, microphone and location are set on every response.
- **Server:**
  - The server speaks plain HTTP and must sit behind a reverse proxy that terminates HTTPS: `public_origin` must be `https://`, and the device cookie is `Secure`.
  - It binds to localhost (or to the address a containerised proxy reaches the host on) and runs as its own user, `isis-push`, in a systemd sandbox that can't see `/home` and can only write to its state and runtime directories. Its code and config are root's, so it can't change them.
  - The shared secret is root's (0600) and reaches the site only as a systemd credential. The VAPID key and database are the site's own, owner-only.

## Setup

The site runs as its own user, `isis-push`, so a compromise can't act as the monitor's user. Its code is a root-owned checkout in `/opt/isis-push-site`, its config and the shared secret are in `/etc/isis-push-site`, and its database and VAPID key are in `/var/lib/isis-push-site`, which systemd creates. The monitor reaches it through `/run/isis-push-site/ingest.sock`, as a member of the `isis-ingest` group. The site needs Python 3.12+ and a public HTTPS name, e.g. `push.example.org`, pointing at the server. Run these as an account that can use `sudo`.

1. **Install the code** (a release tag, as root, so the site can't change it):
   ```bash
   sudo apt install python3-venv   # on Debian/Ubuntu, if venv isn't available yet
   sudo git clone --branch vX.Y.Z https://github.com/MaxPelly/ISIS_Push_Site /opt/isis-push-site
   sudo python3 -m venv /opt/isis-push-site/.venv
   sudo /opt/isis-push-site/.venv/bin/pip install -e /opt/isis-push-site
   sudo chmod -R o+rX /opt/isis-push-site   # in case your umask reaches sudo: isis-push must read it
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin isis-push
   sudo groupadd --system isis-ingest
   sudo usermod -aG isis-ingest MONITOR_USER   # the user the monitor's daemon runs as
   ```

2. **Create the shared secret**, root's alone: systemd hands each service its own copy as a credential (`LoadCredential=`). The monitor's unit loads the same file. Surrounding whitespace is ignored.
   ```bash
   sudo install -d -m 755 /etc/isis-push-site
   sudo sh -c 'umask 077; openssl rand -hex 32 > /etc/isis-push-site/ingest.secret'
   ```

3. **Configure the site:** copy the example, then set `public_origin` (e.g. `https://push.example.org`) and `vapid_subject` (a `mailto:` address push services can use to reach you), and the paths below. Leave `ingest_secret_file` unset: the site reads the credential.
   ```bash
   sudo install -m 644 /opt/isis-push-site/push_site.ini.example /etc/isis-push-site/push_site.ini
   sudoedit /etc/isis-push-site/push_site.ini
   ```
   ```ini
   [SERVER]
   ingest_socket = /run/isis-push-site/ingest.sock
   [SECRETS]
   vapid_key_file = /var/lib/isis-push-site/vapid.pem
   [STORAGE]
   db_path = /var/lib/isis-push-site/push_site.db
   ```

4. **Run it:**
   - Install the admin wrapper and the unit, create the state directory (systemd keeps it from then on) and the VAPID key (once: replacing it makes every device subscribe again), and start the site:
     ```bash
     sudo install -m 755 /opt/isis-push-site/deploy/isis-push /usr/local/bin/isis-push
     sudo install -m 644 /opt/isis-push-site/deploy/isis-push-site.service /etc/systemd/system/
     sudo systemctl daemon-reload
     sudo install -d -m 700 -o isis-push -g isis-ingest /var/lib/isis-push-site
     isis-push vapid
     sudo systemctl enable --now isis-push-site
     ```
     Logs are in `journalctl -u isis-push-site`.
   - Set up Caddy with `deploy/Caddyfile`, using your host name. That file assumes Cloudflare's proxy is in front (see below). Without Cloudflare, delete its `tls` line and Caddy gets a certificate itself.
   - Any reverse proxy works if it terminates HTTPS, forwards to `127.0.0.1:8765`, and refuses `/ingest` and `/ingest/*` (which the public listener doesn't have anyway).
   - If Caddy runs in Docker, set `host` to the Docker bridge's gateway address (`docker network inspect bridge`, usually `172.17.0.1`) and proxy to that address and port. Keep port 8765 closed to other hosts in the server's (and any cloud provider's) firewall: the pages and API are plain HTTP there, without the proxy.

5. **Point the monitor at it** (monitor 2.1.0 or later): in its config, set `[PUSH] url = unix:/run/isis-push-site/ingest.sock`, give its unit `LoadCredential=push-secret:/etc/isis-push-site/ingest.secret` and `SupplementaryGroups=isis-ingest` (the monitor's `deploy/isis-beam-monitor.service` has both), then restart it.

### Behind Cloudflare's proxy

With a proxied (orange-cloud) DNS record in front of Caddy, set `public_origin` to the Cloudflare hostname as usual. Then, in Cloudflare:

- **Certificate:**
  - Set SSL/TLS mode to **Full (strict)**.
  - Create an **Origin Certificate** (SSL/TLS > Origin Server), save it as `/etc/caddy/cf-origin.pem` and `/etc/caddy/cf-origin.key`, and make both readable by the `caddy` user.
  - Caddy's HTTP and TLS-ALPN challenges don't get through the proxy. Alternatively, use Let's Encrypt's DNS challenge (`acme_dns cloudflare`, with a Caddy build that has the Cloudflare DNS module) and drop the `tls` line.
- **For this hostname, turn off anything that rewrites pages:**
  - Rocket Loader
  - Email Obfuscation
  - Web Analytics auto-injection
  - Bot Fight Mode, or any challenge covering `/api/*`

  The first three inject or rewrite scripts, which the site's Content Security Policy blocks. A challenge page breaks the app's API calls.
- **Caching:** don't add "Cache Everything" rules. Cloudflare's default follows the site's `Cache-Control` headers, which keep pages, scripts and API responses uncached.
- **Optional extras:**
  - A rate limiting rule on `/api/*`. An open page reads `/api/status` every 5 seconds (12 a minute), and phones on one network (e.g. a site's Wi-Fi) share an IP, so allow for several: e.g. 300 requests a minute per IP.
  - A WAF rule blocking `/ingest` and `/ingest/*`.
  - Allowing ports 80 and 443 on the server only from Cloudflare's IP ranges. Other Cloudflare accounts can still reach the origin through Cloudflare, bypassing your rules; Authenticated Origin Pulls with your own certificate closes that.

Nothing in the site or the monitor needs changing: the monitor posts over the Unix socket, which no request through Cloudflare can reach.

## Inviting people

The admin commands run as `isis-push`, which owns the database. `deploy/isis-push` (installed as `/usr/local/bin/isis-push` in step 4, the path `deploy/isis-push.sudoers` names) runs them from any directory and switches to that user with `sudo`. Edit its `site=`, `config=` and `user=` lines if your layout differs.

Then, as root or any account that can use `sudo`:

```bash
isis-push invite --label "Alex's phone"
# --uses N lets N devices join with one link; --days D sets how long it's valid (default 7)
isis-push devices
isis-push revoke 3
```

To let accounts without full `sudo` (such as the monitor's user) run these, install `deploy/isis-push.sudoers`. Its comments give the steps. It lets them mint invite links, list and revoke devices, and create a VAPID key if there's none, nothing more. Without the wrapper, the same commands are `sudo -u isis-push /opt/isis-push-site/.venv/bin/python -m push_site invite /etc/isis-push-site/push_site.ini ...`, run from `/opt/isis-push-site`.

The admin commands refuse to run as root, since files they created would then be unwritable by the site. The wrapper switches to `isis-push` first, so running it as root works.

Send the link it prints. The person receiving it then does one of the following:

- **Android or desktop:** open the link, tap **Join**, then **Turn on notifications**. You can optionally install the app from the browser menu.
- **iPhone or iPad (iOS 16.4+):**
  1. Copy the link.
  2. Open the site in Safari and tap Share, then **Add to Home Screen**.
  3. Open the app from the Home Screen, paste the link, and tap **Join**, then **Turn on notifications**.

  Web Push only works in Home Screen apps on iOS, and the app may not share Safari's cookies, so joining happens inside the app.

## The Status section

The top of the app shows the monitor's latest status, from `/ingest/status` (below), to signed-in devices only. It's read every 5 seconds while the page is visible; the monitor sends it within 5 seconds of a change, so it's about 5 seconds behind.

- **Beams:** each one's power, current and since when, with its last 24h as a line coloured by power.
- **Instruments:** each one's run number and state, title, and counts against its target as a progress bar (`≈` marks a target the monitor estimated).
- **MCR news.**
- **When it was updated**, in the device's time.

Values the monitor couldn't read, e.g. while a PV or an instrument's IOC is down, show as `—`, a gap in the line, an indeterminate progress bar or the run state "unknown". If no status has come for 3 of the monitor's update intervals, the section is greyed out and says "Monitor offline since" the last one's arrival. If the site can't be reached, it's greyed out and says when it was last updated. Until the monitor sends its first status, it says "Nothing from the monitor yet."

## The ingest payload (version 1)

`POST /ingest` with `Content-Type: application/json`, plus these headers:

- `X-Timestamp`: Unix seconds, as plain digits
- `X-Signature`: hex `HMAC-SHA256(secret, "<X-Timestamp>.<body>")`

The body is UTF-8 JSON, at most 64 KB:

```json
{
  "v": 1,
  "id": "<uuid4: lower-case hex and dashes>",
  "title": "...", "text": "...",
  "severity": "info | good | warning | attention",
  "emoji": "...", "facts": [["key", "value"]],
  "url": "https://... or null", "url_label": "Open",
  "timestamp": "<ISO 8601> or null",
  "topic": "TS1 | TS2 | Muons | MCR | Summary | <instrument name>",
  "monitor_version": "1.0.0"
}
```

Only `v`, `id` and `severity` are required; missing text fields are empty and `facts` an empty list. `topic` is at most 64 characters, and `url` must be `http://` or `https://`. `monitor_version` is up to 32 letters, digits, `.`, `+` and `-`; the page footer shows the newest notification's. The site ignores unknown fields. A reply of 204 means the notification was accepted, including when its `id` was already seen. Other replies:

- 400: a malformed payload
- 401: a bad or stale signature
- 404: not over the ingest socket (e.g. through the public listener)
- 413: a body over 64 KB
- 503: the database was busy (e.g. with an admin command); retry

A push carries only the id, the severity, the title (the emoji and title, cut to 120 characters) and the text (cut to 300 characters, ending in "…" when cut), which keeps it well under Web Push's 4 KB. The feed shows the whole notification. Warnings and attention notifications are sent as high urgency, and a push service keeps trying an offline device for an hour. Tapping a push opens the app at that notification's feed entry.

## The status payload (version 1)

`POST /ingest/status`, signed in the same way as `/ingest`, carries the monitor's latest view: beam currents and their last 24h, each instrument's run, the MCR news and the monitor's health. Its layout is documented in `push_site/status.py`, which checks every field and bounds every list and string. The site keeps only the newest one (by `generated_at`; an older or repeated one gets 409, and one more than 60 seconds in the future 400), in memory and in its database (saved at most once a minute, and on shutdown), so a restart can serve it straight away. Signed-in devices read it from `GET /api/status` as `{"status": ..., "received_at", "age", "stale"}`, or `{"status": null}` before the first. `age` is seconds since it arrived, by the site's clock, and `stale` means it's older than 3 of its `interval_s`: the monitor is offline. Your reverse proxy must refuse `/ingest/*` as well as `/ingest` (the example Caddyfile does).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The code is in `push_site/`:

| File | Contents |
|---|---|
| `config.py` | the INI file |
| `storage.py` | SQLite |
| `app.py` | routes, ingest and the API |
| `prefs.py` | topics and quiet hours |
| `push.py` | VAPID and sending |
| `status.py` | the status payload |
| `__main__.py` | the command line |
| `static/` | the web app |

### Trying it in a browser

The site doesn't work over plain `http://`, even on `localhost`. Chromium does keep `Secure` cookies such as the device cookie on `http://localhost`, but the API refuses any change whose `Origin` isn't `public_origin`, which must be `https://`, so joining fails. Run it behind HTTPS even locally, e.g. `caddy reverse-proxy --from localhost:8443 --to 127.0.0.1:8765` (Caddy signs `localhost` with its own local CA) with `public_origin = https://localhost:8443`. Phones won't trust that CA, so test on a phone against a real HTTPS name.

## Releases

Versions follow [Semantic Versioning](https://semver.org/) and are set in `push_site/__init__.py` (`__version__`), which the page footer and `--version` show. To release:

1. Move the `[Unreleased]` entries in `CHANGELOG.md` into a new `## [X.Y.Z] - YYYY-MM-DD` section, and set `__version__` to `X.Y.Z`.
2. Commit, then tag and push: `git tag -a vX.Y.Z -m vX.Y.Z && git push origin main vX.Y.Z`.

The release workflow checks the tag matches `__version__`, runs the tests and publishes a GitHub release with that section's notes (a pre-release for tags ending in `aN`, `bN` or `rcN`). Then install the tag as in [Setup](#setup) (`sudo git -C /opt/isis-push-site fetch --tags && sudo git -C /opt/isis-push-site checkout vX.Y.Z`, run its `pip install -e /opt/isis-push-site` again if the dependencies changed, and restart the site).
