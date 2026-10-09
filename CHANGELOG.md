# Changelog

All notable changes to the push site are recorded here. Versions follow
[Semantic Versioning](https://semver.org/); a release is a `vX.Y.Z` tag, and
its notes are copied from the matching section below.

## [Unreleased]

- The page footer also shows the monitor's version, from the newest notification's optional `monitor_version` field.

## [1.0.0] - 2026-10-09

First versioned release.

- Signed `/ingest` endpoint (payload version 1) for the monitor's `[PUSH]` webhook.
- Invite-only devices, per-device topics and quiet hours, and a recent-notifications feed.
- Web Push delivery through the browsers' push services, with a VAPID key created on first install.
- The site's version is shown in the page footer and by `python -m push_site --version`.
