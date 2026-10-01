# ISIS Push Site

Mobile push notifications for the ISIS Beam Monitor, as an
installable web app (PWA). The monitor daemon POSTs each notification to this site
as HMAC-signed JSON; the site stores a short feed and sends Web Push messages to
the devices subscribed to that notification's topic.

It is an optional submodule of the monitor: it never imports the monitor's code,
and the signed JSON payload is the only contract between them.

Setup and deployment are documented once the site is complete.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```
