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
  - The server binds to localhost and runs as its own user in a systemd sandbox.
  - Secrets, the VAPID key and the database are owner-only files.

## Setup

These steps assume the monitor is checked out at `/opt/ISIS_Beam_Monitor` with this repo as its `push_site` submodule (`git submodule update --init push_site`). The site needs Python 3.10+ and a public HTTPS name, e.g. `push.example.org`, pointing at the server.

1. **Install the code and a user to run it.** The `pushsite` user must be able to read the checkout (the default umask 022 allows this).
   ```bash
   sudo apt install python3-venv   # on Debian/Ubuntu, if venv isn't available yet
   cd /opt/ISIS_Beam_Monitor/push_site
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   sudo useradd --system --home /var/lib/isis-push-site --shell /usr/sbin/nologin pushsite
   sudo install -d -o pushsite -g pushsite -m 700 /var/lib/isis-push-site
   ```

2. **Create the shared secret** and give the daemon and the site their own copies, each readable only by the user that needs it. Surrounding whitespace in the files is ignored. The monitor's `.gitignore` covers `*.secret`, so the daemon's copy can't be committed by accident.
   ```bash
   (umask 077; openssl rand -hex 32 > /tmp/push.secret)
   sudo install -o pushsite -g pushsite -m 600 /tmp/push.secret /var/lib/isis-push-site/push.secret
   sudo install -o <daemon-user> -m 600 /tmp/push.secret /opt/ISIS_Beam_Monitor/push.secret
   rm /tmp/push.secret
   ```

3. **Configure the site:**
   - Create the config from the example: `sudo install -o pushsite -g pushsite -m 600 push_site.ini.example /var/lib/isis-push-site/push_site.ini`, then edit it with `sudoedit`.
   - Set `public_origin` (e.g. `https://push.example.org`) and `vapid_subject` (a `mailto:` address push services can use to reach you).
   - Relative paths in it are relative to the file.
   - Create the VAPID key once. Replacing it later makes every device subscribe again.
   ```bash
   sudo -u pushsite .venv/bin/python -m push_site vapid /var/lib/isis-push-site/push_site.ini
   ```

4. **Run it:**
   - Copy `deploy/isis-push-site.service` to `/etc/systemd/system/`, adjusting the paths if needed, then run `sudo systemctl daemon-reload && sudo systemctl enable --now isis-push-site`. Logs are in `journalctl -u isis-push-site`.
   - Set up Caddy with `deploy/Caddyfile`, using your host name. Caddy fetches the certificate itself.
   - Any reverse proxy works if it terminates HTTPS, refuses `/ingest`, adds `X-Forwarded-For`, and forwards the rest to `127.0.0.1:8765`. The site refuses forwarded `/ingest` requests as a backstop.

5. **Point the monitor at it** in the monitor's `config.ini`, then restart the daemon. A relative `secret_file` is relative to the daemon's working directory, so an absolute path is safest:
   ```ini
   [PUSH]
   url = http://127.0.0.1:8765/ingest
   secret_file = /opt/ISIS_Beam_Monitor/push.secret
   ```

## Inviting people

```bash
cd /opt/ISIS_Beam_Monitor/push_site
sudo -u pushsite .venv/bin/python -m push_site invite /var/lib/isis-push-site/push_site.ini --label "Alex's phone"
# --uses N lets N devices join with one link; --days D sets how long it's valid (default 7)
sudo -u pushsite .venv/bin/python -m push_site devices /var/lib/isis-push-site/push_site.ini
sudo -u pushsite .venv/bin/python -m push_site revoke /var/lib/isis-push-site/push_site.ini 3
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
