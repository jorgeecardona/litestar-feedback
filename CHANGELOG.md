# Changelog

All notable changes to `litestar-feedback`. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

First release. Nothing is on PyPI yet.

### Added

- `SupportPlugin`: a customer support inbox with issues, a reply thread and an operator inbox,
  following ISSUES-STANDARD.md.
- Issues move between `open`, `waiting_customer`, `resolved` and `closed`; other moves get 409.
- A customer reply on a closed issue opens a new issue that points to the closed one
  (`follows_id`).
- Waiting and resolved issues move on by themselves after 14 days.
- Operators can write internal notes that customers never see.
- Operators set an issue's kind with `POST /admin/issues/{id}/kind`. The change shows as an
  internal note and does not restart the 14-day timer.
- Customers get a mail (through your mailer) when an operator replies. The mail goes out only
  after the reply is committed.
- Every support error has a stable `code` next to `detail`.
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
