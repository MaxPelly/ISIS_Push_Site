# Changelog

All notable changes to the push site are recorded here. Versions follow
[Semantic Versioning](https://semver.org/); a release is a `vX.Y.Z` tag, and
its notes are copied from the matching section below.

## [Unreleased]

- `deploy/isis-push` wraps the admin commands (`isis-push invite|devices|revoke`) so they run as the site's user from any directory, and `deploy/isis-push.sudoers` optionally lets a group run them without full `sudo`.
- When every device's push is refused with the same HTTP 400, 401 or 403 (or 80% of them, from 5 devices), the site logs one error pointing at its VAPID key or clock and doesn't count it against the devices, so a site fault no longer removes every subscription. A site with one device can't tell the two apart and still drops it after 20 refusals in a row. HTTP 413 is never counted against a device.
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
