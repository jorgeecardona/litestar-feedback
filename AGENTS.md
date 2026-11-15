# AGENTS.md

Guidance for agents working in this repo. Humans: read README.md.

## Overview

`litestar-feedback` ships Litestar plugins. Each plugin lives in its own subpackage and never
imports another plugin:

- `src/litestar_feedback/support/`: `SupportPlugin`, the customer support inbox
  (ISSUES-STANDARD.md).
- `src/litestar_feedback/replay/`: `ReplayPlugin`, rrweb session replay and click heatmaps.
  `events.py` holds the pure rrweb parsing.
- `src/litestar_feedback/caller.py`, `_sqltypes.py`: shared by all plugins.

## Build

1. `proto install` (moon and uv, pinned in `.prototools`).
2. `uv sync`.
3. `make install-hooks` once after cloning.

## Test

```bash
make check
```

- The suite runs the shipped contract (`support/contract.py`) against `examples/support_app.py`.
- Replay tests run against `examples/replay_app.py`. Plugin jobs (`prune`, `delete_user`) run
  through `client.blocking_portal.call(...)`, on the app's event loop.
- Rule tests use `FakeClock` from `tests/conftest.py` to move time. Never sleep.
- Litestar deprecation warnings are errors (`filterwarnings` in `pyproject.toml`).

## Style

- ruff, line length 100. pyright strict on `src`, `tests`, `examples`.
- Frozen dataclasses for data. msgspec structs for HTTP bodies.
- Plain SQLAlchemy async. No Advanced Alchemy.
- Route parameters use explicit markers (`FromPath`, `JSONBody`, `NamedDependency`).
- Plugins subclass `litestar.plugins.InitPlugin`.

## Gotchas

- `issue_events.seq` orders a thread. Never order by `created_at`: two events can share it.
- Status moves only through `SupportService.set_status` or `comment`.
- `last_seen_event_id` is compared with the newest event the caller's side can see. Customers
  never see internal notes, so those are skipped for them.
- Reply mail goes out after the commit: `SupportService` queues notices in `outbox`, and the
  route calls `SupportRuntime.deliver` after its session block. Never call the mailer inside a
  session.
- `updated_at` drives the 14-day timers. An event that is not activity (the kind note) passes
  `touch=False` to `_append`.
- `scope_id` and `user_id` are text so integer keys fit. Hosts pass `str(id)`.
- Replay never imports support. Support hands its replay references over through
  `ReplayAdapter.protected_sessions`.
- Replay deletes chunks and clicks explicitly: SQLite ignores ON DELETE CASCADE by default.
- SQLAlchemy 2.1 deprecates `Result.tuples()`. Deprecation warnings are errors, so a 500 in a
  test can be a warning. Use `.all()`.
- Each `migration.py` writes the tables out by hand. When a model changes, change it too:
  `test_migration_matches_models` fails otherwise.

## Do not

- Add `Co-Authored-By` lines to commits.
- Publish to PyPI or create the GitLab project without Jorge's instruction.
