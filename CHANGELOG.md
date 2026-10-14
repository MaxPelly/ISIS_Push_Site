# Changelog

All notable changes to the push site are recorded here. Versions follow
[Semantic Versioning](https://semver.org/); a release is a `vX.Y.Z` tag, and
its notes are copied from the matching section below.

## [Unreleased]

- A request that finds the database locked for longer than its 1s busy timeout (e.g. by an `isis-push` admin command) gets HTTP 503, which the monitor retries, rather than 500, and the site logs a warning.
## [1.1.4] - 2026-10-15

For monitor 1.3.0rc3. No config change.

- MCR news, and a beam coming back on after being off, are pushed with high urgency as well as warnings and a beam going off, so a phone that's been idle (e.g. in Android's Doze) shows them straight away rather than at its next wake-up.

## [1.1.3] - 2026-10-14

For monitor 1.3.0rc3. No config change.

- Tags ending in `aN`, `bN` or `rcN` are published as GitHub pre-releases, so they don't become "Latest".
- Status: while an instrument's IOC is down, its progress bar is indeterminate rather than empty and its run state reads "unknown"; while the monitor is offline (or the status can't be read), the beams, instruments and news are greyed out, not just the footer.
- Ingest accepts and ignores `flavour`, which the monitor no longer sends, rather than keeping it.
- A status rejected again for the same reason is logged at debug after the first warning.
- Correction to 1.1.0's notes: the monitor's status publisher shipped in monitor 1.3.0rc2, not 1.4.0.

## [1.1.2] - 2026-10-13

- The page asks for its script and styles under a new URL each release (`app.js?v=1.1.2`), so a phone gets them straight away even where a cache in front of the site, such as Cloudflare's Browser Cache TTL, overrides their `no-cache` and keeps the last release's for hours.

## [1.1.1] - 2026-10-13

- The Status section says when the status was last updated ("Updated at 14:32:05", in the device's time) instead of how many seconds ago, which said little between the monitor's once-a-minute updates.

## [1.1.0] - 2026-10-13

Adds a Status page, fed by monitor 1.4.0 or later (an older monitor sends no status, and the page says so). Upgrading: if your reverse proxy refuses `/ingest` by exact path, make it refuse `/ingest/*` too (the example Caddyfile already does).

- The monitor can post its status (beams with their last 24h, each instrument's run, MCR news, health) to `/ingest/status`, signed like notifications; the site keeps the newest and serves it to signed-in devices at `/api/status`, marked stale if the monitor stops sending. A reverse proxy should refuse `/ingest/*` as well as `/ingest`.
- A Status section at the top of the page shows the monitor's latest status: each beam's current, power and last 24h, each instrument's run with its progress to its target (≈ for an auto target), the MCR news, and when it was updated, or that the monitor is offline. It's read every 30 seconds while the page is open.
- The feed's tabs are built from the topics the site knows (`/api/topics` now gives each topic's `id`, `label` and whether it's an instrument's, including any this device chose that aren't offered any more), instead of being written out in the page and its CSS. Feed items no longer show a flavour line, which the monitor stopped sending.

## [1.0.3] - 2026-10-13

Upgrading: in `push_site.ini`, replace any `[STORAGE] feed_max_items` with `feed_items_per_kind`, and remove `feed_max_days = 7` if it's set, so the new 30-day default applies.

- The feed keeps the newest `feed_items_per_kind` (default 75) notifications of each kind (beam, MCR news, daily summary and experiment) instead of the newest 200 overall, so a burst of beam cards no longer pushes the rest out. This replaces `[STORAGE] feed_max_items`, which is now ignored, and `feed_max_days` defaults to 30 instead of 7. The page shows everything kept, so the 1.0.2 entry's "whole stored feed" is now true. The existing feed is pruned to the new rule on the first notification after upgrading.
- Turning notifications on fetches the site's push key first if the page couldn't load it earlier, instead of subscribing without one.
- When the browser replaces a push subscription, the service worker registers the browser's new one rather than making another, and fails visibly if the site refuses it.
- An empty feed says so once.
- `deploy/isis-push` wraps the admin commands (`isis-push invite|devices|revoke`) so they run as the site's user from any directory, and `deploy/isis-push.sudoers` optionally lets a group run them without full `sudo`.
- When every device's push is refused with the same HTTP 400, 401 or 403 (or 80% of them, from 5 devices), the site logs one error pointing at its VAPID key or clock and doesn't count it against the devices, so a site fault no longer removes every subscription. Until a push succeeds again, that refusal isn't counted against any device, even in a push that went to only one. A site with one device can't tell the two apart and still drops it after 20 refusals in a row. HTTP 413 is never counted against a device.
- A device's refused push only removes that device's subscription, not one another device has since claimed with the same endpoint.

## [1.0.2] - 2026-10-10

- The recent-notifications feed has tabs (All, TS1, TS2, Muons, MCR, Summary and Experiments) to read the history of one kind of notification, and shows the whole stored feed rather than the newest 50.

## [1.0.1] - 2026-10-09

- The page footer also shows the monitor's version, from the newest notification's optional `monitor_version` field.

## [1.0.0] - 2026-10-09

First versioned release.

- Signed `/ingest` endpoint (payload version 1) for the monitor's `[PUSH]` webhook.
- Invite-only devices, per-device topics and quiet hours, and a recent-notifications feed.
- Web Push delivery through the browsers' push services, with a VAPID key created on first install.
- The site's version is shown in the page footer and by `python -m push_site --version`.
