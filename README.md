>  **Note:** This branch includes AI-assisted code generation. Treat it with appropriate caution.

# ISIS Push Site

Mobile push notifications for the ISIS Beam Monitor, as a small installable web app (PWA).

The monitor daemon POSTs each notification to this site as HMAC-signed JSON. The site keeps a short feed of them and sends Web Push messages to each device that wants that notification's topic. People join with an invite link, choose topics (TS1, TS2, Muons, MCR news, the daily summary, individual instruments) and optional quiet hours, and can read recent notifications in the app.

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
  - `/ingest` needs a valid `X-Signature` (HMAC-SHA256 of `<X-Timestamp>.<body>` under the shared secret) and a timestamp within 60 seconds. A repeated notification id is dropped.
  - Requests forwarded by the proxy are refused, and the proxy refuses the path anyway.
- **Accounts:**
  - There are none. An admin creates invite links on the server, and redeeming one gives that browser a random device token in a `__Host-` cookie (`Secure`, `HttpOnly`, `SameSite=Strict`). A device that goes unused for a year is signed out.
  - Only SHA-256 hashes of tokens are stored, and devices can be listed and revoked from the command line.
  - The invite token sits in the link's `#fragment`, so it never reaches server or proxy logs.
- **API:**
  - Every change must be JSON from the site's own origin.
  - Push subscriptions are only accepted for the browsers' push services (FCM, Apple, Mozilla, Windows), with keys that are checked to be valid. This means the site can't be used to send requests to arbitrary servers.
- **Pages:**
  - The pages have a strict Content Security Policy (no inline script or style) and set all text with `textContent`.
  - `nosniff`, `Referrer-Policy: same-origin` and `frame-ancestors 'none'` are set on every response.
- **Server:**
  - The server binds to localhost (or to every interface, for a reverse proxy in a container) and runs in a systemd sandbox that can only write to its own directory.
  - `/ingest` only answers requests from a loopback address that carry no proxy headers, whatever the server binds to.
  - Secrets, the VAPID key and the database are owner-only files.

## Setup

These steps run everything as the `claude` user, with the monitor checked out at `/home/claude/ISIS_Beam_Monitor` and this repo as its `push_site` submodule (`git submodule update --init push_site`). The site's config, secret, VAPID key and database all live in `/home/claude/ISIS_Beam_Monitor/push_site`, and git ignores them. The site needs Python 3.10+ and a public HTTPS name, e.g. `push.example.org`, pointing at the server.

1. **Install the code:**
   ```bash
   sudo apt install python3-venv   # on Debian/Ubuntu, if venv isn't available yet
   cd /home/claude/ISIS_Beam_Monitor/push_site
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```

2. **Create the shared secret.** The daemon runs as the same user, so it reads this same file. Surrounding whitespace in it is ignored.
   ```bash
   (umask 077; openssl rand -hex 32 > push.secret)
   ```

3. **Configure the site:**
   - Create the config from the example with `(umask 077; cp push_site.ini.example push_site.ini)`.
   - Set `public_origin` (e.g. `https://push.example.org`) and `vapid_subject` (a `mailto:` address push services can use to reach you).
   - Relative paths in it are relative to the file.
   - Create the VAPID key once. Replacing it later makes every device subscribe again.
   ```bash
   .venv/bin/python -m push_site vapid push_site.ini
   ```

4. **Run it:**
   - Copy `deploy/isis-push-site.service` to `/etc/systemd/system/`, then run `sudo systemctl daemon-reload && sudo systemctl enable --now isis-push-site`. Logs are in `journalctl -u isis-push-site`.
   - Set up Caddy with `deploy/Caddyfile`, using your host name. That file assumes Cloudflare's proxy is in front (see below). Without Cloudflare, delete its `tls` line and Caddy gets a certificate itself.
   - Any reverse proxy works if it terminates HTTPS, refuses `/ingest`, adds `X-Forwarded-For`, and forwards the rest to `127.0.0.1:8765`. The site refuses forwarded `/ingest` requests as a backstop.
   - If Caddy runs in Docker, set `host = 0.0.0.0` and proxy to `host.docker.internal:8765` (with `extra_hosts: ["host.docker.internal:host-gateway"]` on Linux). The site still only takes `/ingest` from loopback. Keep port 8765 closed in the server's firewall.

5. **Point the monitor at it** in the monitor's `config.ini`, then restart the daemon:
   ```ini
   [PUSH]
   url = http://127.0.0.1:8765/ingest
   secret_file = /home/claude/ISIS_Beam_Monitor/push_site/push.secret
   ```

### Behind Cloudflare's proxy

With a proxied (orange-cloud) DNS record in front of Caddy, set `public_origin` to the Cloudflare hostname as usual. Then, in Cloudflare:

- **Certificate:**
  - Set SSL/TLS mode to **Full (strict)**.
  - Create an **Origin Certificate** (SSL/TLS > Origin Server), save it as `/etc/caddy/cf-origin.pem` and `/etc/caddy/cf-origin.key`, and make both readable by the `caddy` user.
  - Caddy's own Let's Encrypt challenges don't get through the proxy.
- **For this hostname, turn off anything that rewrites pages:**
  - Rocket Loader
  - Email Obfuscation
  - Web Analytics auto-injection
  - Bot Fight Mode, or any challenge covering `/api/*`

  The first three inject or rewrite scripts, which the site's Content Security Policy blocks. A challenge page breaks the app's API calls.
- **Caching:** don't add "Cache Everything" rules. Cloudflare's default follows the site's `Cache-Control` headers, which keep pages, scripts and API responses uncached.
- **Optional extras:**
  - A rate limiting rule on `/api/*`, e.g. 60 requests a minute per IP.
  - A WAF rule blocking `/ingest`.
  - Allowing port 443 on the server only from Cloudflare's IP ranges, or turning on Authenticated Origin Pulls.

Nothing in the site or the monitor needs changing. The daemon still posts to `127.0.0.1` directly, and the site only takes `/ingest` from loopback without proxy headers such as Cloudflare's `CF-Connecting-IP`.

## Inviting people

From `/home/claude/ISIS_Beam_Monitor/push_site`:

```bash
.venv/bin/python -m push_site invite push_site.ini --label "Alex's phone"
# --uses N lets N devices join with one link; --days D sets how long it's valid (default 7)
.venv/bin/python -m push_site devices push_site.ini
.venv/bin/python -m push_site revoke push_site.ini 3
```

The admin commands refuse to run as root, since files they created would then be unwritable by the site.

Send the link it prints. The person receiving it then does one of the following:

- **Android or desktop:** open the link, tap **Join**, then **Turn on notifications**. You can optionally install the app from the browser menu.
- **iPhone or iPad (iOS 16.4+):**
  1. Copy the link.
  2. Open the site in Safari and tap Share, then **Add to Home Screen**.
  3. Open the app from the Home Screen, paste the link, and tap **Join**, then **Turn on notifications**.

  Web Push only works in Home Screen apps on iOS, and the app may not share Safari's cookies, so joining happens inside the app.

## The ingest payload (version 1)

`POST /ingest` with `Content-Type: application/json`, plus these headers:

- `X-Timestamp`: Unix seconds, as plain digits
- `X-Signature`: hex `HMAC-SHA256(secret, "<X-Timestamp>.<body>")`

The body is UTF-8 JSON, at most 64 KB:

```json
{
  "v": 1,
  "id": "<uuid4: lower-case hex and dashes>",
  "title": "...", "text": "...", "summary": "...",
  "severity": "info | good | warning | attention",
  "emoji": "...", "facts": [["key", "value"]], "flavour": "",
  "url": "https://... or null", "url_label": "Open",
  "timestamp": "<ISO 8601> or null",
  "channel": "...",
  "topic": "TS1 | TS2 | Muons | MCR | Summary | <instrument name>"
}
```

The site ignores unknown fields. A reply of 204 means the notification was accepted, including when its `id` was already seen. Other replies:

- 400: a malformed payload
- 401: a bad or stale signature
- 413: a body over 64 KB
- 404: a request that came through the proxy

Pushes carry only the title, the first 300 characters of the text, the severity and the id. Tapping one opens the app at that notification's feed entry.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

The code is in `push_site/`:

| File | Contents |
|---|---|
| `config.py` | the INI file |
| `storage.py` | SQLite |
| `app.py` | routes, ingest and the API |
| `prefs.py` | topics and quiet hours |
| `push.py` | VAPID and sending |
| `__main__.py` | the command line |
| `static/` | the web app |
