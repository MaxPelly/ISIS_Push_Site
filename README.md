>  **Note:** This branch includes AI-assisted code generation. Treat it with appropriate caution.

# ISIS Push Site

Mobile push notifications for the ISIS Beam Monitor, as a small installable web app (PWA).

The monitor daemon POSTs each notification to this site as HMAC-signed JSON. The site keeps a short feed of them and sends Web Push messages to each device that wants that notification's topic. People join with an invite link, choose topics (TS1, TS2, Muons, MCR news, the daily summary, individual instruments) and optional quiet hours, and can read recent notifications in the app. The feed keeps the newest 75 of each kind (beam, MCR news, daily summary and experiment notifications) for up to 30 days, set by `[STORAGE] feed_items_per_kind` and `feed_max_days`. The monitor also posts its status every minute or so, which the app's Status section shows (see [The Status section](#the-status-section)).

It is an optional submodule of the monitor. It never imports the monitor's code, and the signed JSON payload (described below) is the only contract between them.

## How it fits together

```
monitor daemon ──signed POST──► 127.0.0.1:8765/ingest ─┐
                                                       ├─ push_site (aiohttp + SQLite)
phones ──HTTPS──► Caddy (TLS, refuses /ingest) ────────┘        │
                                                                └─► Web Push (Apple / Google / Mozilla) ──► phones
```

### Security model

- **Ingest:**
  - `/ingest` and `/ingest/status` are only for the monitor daemon on the same host. The reverse proxy must refuse both (`/ingest` and `/ingest/*`).
  - As a backstop, the site answers them (otherwise with 404) only when the direct peer is a loopback address and the request has none of the headers a proxy adds (`X-Forwarded-For`, `Forwarded`, `CF-Connecting-IP`), whatever address the server binds to.
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
  - It binds to localhost (or to every interface, for a reverse proxy in a container) and runs as its own user in a systemd sandbox that can't see the rest of `/home` and can only write to its `data/` directory.
  - Secrets, the VAPID key and the database are owner-only files, with read access for the site's user granted by ACL.

## Setup

These steps assume the monitor is checked out at `$MONITOR_DIR`, run by its own user, with this repo as its `push_site` submodule (`git submodule update --init push_site`). **Set `MONITOR_DIR` to your checkout first**, in the shell you run the steps from, e.g. `MONITOR_DIR=/home/monitor/ISIS_Beam_Monitor`. The site runs as its own user, `isis-push`, with read-only access to its code, config, secret and VAPID key granted by ACLs. Sharing the monitor's user, or a group with write access to its files, would let a compromised site act as it (e.g. through another process's `/proc/<pid>/root`). The config, secret and VAPID key live in `$MONITOR_DIR/push_site`, and the database in `data/` there, owned by `isis-push`. Git ignores them all. The site needs Python 3.12+ and a public HTTPS name, e.g. `push.example.org`, pointing at the server.

1. **Install the code:**
   ```bash
   sudo apt install python3-venv   # on Debian/Ubuntu, if venv isn't available yet
   cd "$MONITOR_DIR/push_site"
   python3 -m venv .venv && .venv/bin/pip install -e .
   sudo apt install acl                # for setfacl
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin isis-push
   # Read-only access for isis-push: traverse (no listing) on the checkout and its
   # parent; add any other parent directory others can't enter. Then read on the
   # code and venv, including files added later (d:).
   sudo setfacl -m u:isis-push:x "$MONITOR_DIR" "$(dirname "$MONITOR_DIR")"
   setfacl -R -m u:isis-push:rX -m d:u:isis-push:rX .
   sudo install -d -m 700 -o isis-push -g isis-push data   # the only directory the unit can write to
   ```

2. **Create the shared secret.** The monitor's daemon, which owns this directory, reads this same file. Surrounding whitespace in it is ignored. The directory's default ACL overrides the umask, so set each new secret file's mode explicitly:
   ```bash
   openssl rand -hex 32 > push.secret
   chmod 600 push.secret && setfacl -m u:isis-push:r push.secret
   ```

3. **Configure the site:**
   - Create the config from the example with `cp push_site.ini.example push_site.ini`.
   - Set `public_origin` (e.g. `https://push.example.org`) and `vapid_subject` (a `mailto:` address push services can use to reach you).
   - Relative paths in it are relative to the file.
   - Create the VAPID key once. Replacing it later makes every device subscribe again.
   ```bash
   .venv/bin/python -m push_site vapid push_site.ini
   chmod 600 push_site.ini vapid.pem && setfacl -m u:isis-push:r push_site.ini vapid.pem   # chmod 600 alone would cut isis-push off
   ```

4. **Run it:**
   - Install `deploy/isis-push-site.service`, filling in its `/path/to/ISIS_Beam_Monitor` placeholder (if your venv isn't `push_site/.venv`, change `ExecStart` and add the venv to `BindReadOnlyPaths`):
     ```bash
     sed "/^#/!s|/path/to/ISIS_Beam_Monitor|$MONITOR_DIR|g" deploy/isis-push-site.service | sudo tee /etc/systemd/system/isis-push-site.service
     sudo systemctl daemon-reload && sudo systemctl enable --now isis-push-site
     ```
     Logs are in `journalctl -u isis-push-site`.
   - Set up Caddy with `deploy/Caddyfile`, using your host name. That file assumes Cloudflare's proxy is in front (see below). Without Cloudflare, delete its `tls` line and Caddy gets a certificate itself.
   - Any reverse proxy works if it terminates HTTPS, refuses `/ingest` and `/ingest/*`, adds `X-Forwarded-For` (Caddy does), and forwards the rest to `127.0.0.1:8765`. The site refuses `/ingest*` requests that carry proxy headers as a backstop.
   - If Caddy runs in Docker, set `host = 0.0.0.0` and proxy to `host.docker.internal:8765` (with `extra_hosts: ["host.docker.internal:host-gateway"]` on Linux). `/ingest*` still only answers loopback, but the pages and API would be reachable over plain HTTP on port 8765 from anywhere that can reach it, bypassing the proxy. Keep that port closed to other hosts in the server's (and any cloud provider's) firewall, open only to the container network.

5. **Point the monitor at it** in the monitor's `config.ini`, then restart the daemon:
   ```ini
   [PUSH]
   url = http://127.0.0.1:8765/ingest
   # Relative to config.ini's directory
   secret_file = push_site/push.secret
   ```

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
  - A rate limiting rule on `/api/*`, e.g. 60 requests a minute per IP.
  - A WAF rule blocking `/ingest` and `/ingest/*`.
  - Allowing ports 80 and 443 on the server only from Cloudflare's IP ranges. Other Cloudflare accounts can still reach the origin through Cloudflare, bypassing your rules; Authenticated Origin Pulls with your own certificate closes that.

Nothing in the site or the monitor needs changing. The daemon still posts to `127.0.0.1` directly, and the site only takes `/ingest*` from loopback without proxy headers such as Cloudflare's `CF-Connecting-IP`.

## Inviting people

The admin commands run as `isis-push`, which owns the database. `deploy/isis-push` wraps them so they work from any directory and switch to that user with `sudo`. Install it once from `$MONITOR_DIR/push_site`, at `/usr/local/bin/isis-push` (the path `deploy/isis-push.sudoers` names). Change the `python=` line if your venv isn't `push_site/.venv`, and the `user=` line if the site runs as another user:

```bash
sed -e "/^#/!s|/path/to/ISIS_Beam_Monitor|$MONITOR_DIR|g" \
    -e "s|^python=.*|python=$MONITOR_DIR/push_site/.venv/bin/python|" \
    -e "s|^user=.*|user=isis-push|" deploy/isis-push \
  | sudo tee /usr/local/bin/isis-push >/dev/null && sudo chmod 755 /usr/local/bin/isis-push
```

Then, as root or any account that can use `sudo`:

```bash
isis-push invite --label "Alex's phone"
# --uses N lets N devices join with one link; --days D sets how long it's valid (default 7)
isis-push devices
isis-push revoke 3
```

To let accounts without full `sudo` (such as the monitor's user) run these, install `deploy/isis-push.sudoers`. Its comments give the steps. It lets them mint invite links and list and revoke devices, nothing more. Without the wrapper, the same commands are `sudo -u isis-push .venv/bin/python -m push_site invite push_site.ini ...`, run from `$MONITOR_DIR/push_site`.

The admin commands refuse to run as root, since files they created would then be unwritable by the site. The wrapper switches to `isis-push` first, so running it as root works.

Send the link it prints. The person receiving it then does one of the following:

- **Android or desktop:** open the link, tap **Join**, then **Turn on notifications**. You can optionally install the app from the browser menu.
- **iPhone or iPad (iOS 16.4+):**
  1. Copy the link.
  2. Open the site in Safari and tap Share, then **Add to Home Screen**.
  3. Open the app from the Home Screen, paste the link, and tap **Join**, then **Turn on notifications**.

  Web Push only works in Home Screen apps on iOS, and the app may not share Safari's cookies, so joining happens inside the app.

## The Status section

The top of the app shows the monitor's latest status, from `/ingest/status` (below), to signed-in devices only. It's read every 30 seconds while the page is visible.

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
- 404: not a direct request from loopback (another host, or one with proxy headers)
- 413: a body over 64 KB
- 503: the database was busy (e.g. with an admin command); retry

A push carries only the id, the severity, the title (the emoji and title, cut to 120 characters) and the text (cut to 300 characters, ending in "…" when cut), which keeps it well under Web Push's 4 KB. The feed shows the whole notification. Warnings and attention notifications are sent as high urgency, and a push service keeps trying an offline device for an hour. Tapping a push opens the app at that notification's feed entry.

## The status payload (version 1)

`POST /ingest/status`, signed in the same way as `/ingest`, carries the monitor's latest view: beam currents and their last 24h, each instrument's run, the MCR news and the monitor's health. Its layout is documented in `push_site/status.py`, which checks every field and bounds every list and string. The site keeps only the newest one (by `generated_at`; an older or repeated one gets 409, and one more than 60 seconds in the future 400), in its database, so a restart can serve it straight away. Signed-in devices read it from `GET /api/status` as `{"status": ..., "received_at", "age", "stale"}`, or `{"status": null}` before the first. `age` is seconds since it arrived, by the site's clock, and `stale` means it's older than 3 of its `interval_s`: the monitor is offline. Your reverse proxy must refuse `/ingest/*` as well as `/ingest` (the example Caddyfile does).

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

The release workflow checks the tag matches `__version__`, runs the tests and publishes a GitHub release with that section's notes (a pre-release for tags ending in `aN`, `bN` or `rcN`). Then point the monitor's `push_site` submodule at the tag.
