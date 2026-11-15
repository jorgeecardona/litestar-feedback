# Changelog

All notable changes to `litestar-feedback`. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.0] - 2026-11-15

### Added

- A customer who writes on a closed issue gets 409 with `code: "issue_closed"`, and can open a
  new issue that points to it with `follows_id`.
- Operators set an issue's kind with `POST /admin/issues/{id}/kind`. The change shows as an
  internal note and does not restart the 14-day timer.
- Every support error has a stable `code` next to `detail`.
- The contract test has 9 more tests (57 in all). Products that run it need these routes.

### Fixed

- A customer could not resolve an issue after an operator wrote an internal note on it: the
  resolve answered 409 every time.

## [0.1.1] - 2026-11-15

### Added

- `SupportPlugin`: a customer support inbox with issues, a reply thread and an operator inbox,
  following ISSUES-STANDARD.md.
- Issues move between `open`, `waiting_customer`, `resolved` and `closed`; other moves get 409.
- Waiting and resolved issues move on by themselves after 14 days.
- Operators can write internal notes that customers never see.
- Customers get a mail (through your mailer) when an operator replies.
- Screenshots upload first and attach by id; uploads need a store you provide.
- Customer messages are rate limited per user.
- An Alembic helper creates the three tables.
- The standard's contract test ships as a pytest plugin you run against your own app.
- `ReplayPlugin`: stores rrweb session recordings sent in chunks, one per page load.
- Nobody can write into another user's recording.
- Chunk and session size caps.
- Playback can start at a moment, for example the moment a support issue was filed.
- Click heatmaps per page and viewport width.
- Old recordings are pruned; recordings that live issues point to are kept.
- A user's recordings can be deleted with the user.
- An optional consent check runs before each chunk is stored.
