# litestar-feedback

Litestar plugins for an in-app customer support inbox and session replay. `SupportPlugin`
stores customer issues with a reply thread and an operator inbox. `ReplayPlugin` stores rrweb
recordings, plays them back from a moment, and builds click heatmaps. Each plugin works alone.

## Install

```bash
uv add litestar-feedback
```

For replay, install the `replay` extra (empty today, kept for future dependencies):

```bash
uv add "litestar-feedback[replay]"
```

Requires Python 3.12+, Litestar 2.23+ and SQLAlchemy 2.0 async. No Advanced Alchemy.

## Support inbox

`SupportPlugin` follows ISSUES-STANDARD.md: tables `issues`, `issue_events`,
`issue_attachments`, and routes under `/issues` and `/admin/issues`.

- Statuses `open`, `waiting_customer`, `resolved`, `closed`. A move not allowed is 409.
- `closed` is final. A comment on a closed issue is 409.
- A customer comment reopens a `waiting_customer` or `resolved` issue.
- A `waiting_customer` or `resolved` issue moves on after 14 days with no activity.
- Internal notes (`internal: true`) never reach customer routes.
- A public operator reply calls your mailer. The event records `notified_at` or the reason it
  was skipped.
- Customer messages are rate limited per actor (20 per hour by default). Over the limit is 429.
- Images are uploaded first, then attached by id. No store configured means 503.

### Wire it

1. Write a function that turns a request into a `Caller`.
2. Write a function that opens a session for that caller.
3. Add the plugin.

```python
from contextlib import asynccontextmanager

from litestar import Litestar, Request
from litestar.exceptions import NotAuthorizedException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from litestar_feedback.support import Caller, SupportAdapter, SupportConfig, SupportPlugin

engine = create_async_engine("postgresql+psycopg://app@db/app")
sessions = async_sessionmaker(engine, expire_on_commit=False)


async def caller(request: Request) -> Caller:
    user = request.user  # however your app signs users in
    if user is None:
        raise NotAuthorizedException()
    return Caller(
        scope_id=str(user.tenant_id),
        user_id=str(user.id),
        actor=user.email,
        is_operator=user.is_staff,
    )


@asynccontextmanager
async def session(caller: Caller):
    async with sessions.begin() as s:
        if not caller.is_operator:
            await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": caller.scope_id})
        yield s


app = Litestar(
    plugins=[
        SupportPlugin(
            adapter=SupportAdapter(caller=caller, session=session),
            config=SupportConfig(path="/v1"),
        )
    ]
)
```

A runnable version is [examples/support_app.py](examples/support_app.py).

### Adapter

| Field | Type | What it does |
|---|---|---|
| `caller` | `async (Request) -> Caller` | Who is calling. Raise `NotAuthorizedException` for nobody. |
| `session` | `(Caller) -> async context manager of AsyncSession` | One transaction per request. Commit on exit, roll back on error. Set row-level security here. |
| `mailer` | `Mailer` or None | `async send_reply(ReplyNotice) -> bool`. False means the customer has no address. |
| `storage` | `AttachmentStore` or None | `async put`, `delete`, `presign_get`. None makes uploads return 503. |

`Caller` has `scope_id: str`, `user_id: str | None`, `actor: str`, `is_operator: bool`. The ids
are strings so UUID and integer keys fit the same columns.

`SupportConfig` holds `path`, the two timers, the rate limit, upload limits, `system_actor`,
`list_limit` and `clock`. Defaults follow the standard.

### Routes

| Method | Path | Who |
|---|---|---|
| POST | `/issues` | customer |
| GET | `/issues` | customer |
| GET | `/issues/{id}` | customer |
| POST | `/issues/{id}/comments` | customer |
| POST | `/issues/{id}/resolve` | customer |
| POST | `/issue-attachments` | customer |
| GET | `/admin/issues` | operator |
| GET | `/admin/issues/{id}` | operator |
| POST | `/admin/issues/{id}/comments` | operator |
| POST | `/admin/issues/{id}/status` | operator |

### Tables

Call the helper from an Alembic migration:

```python
from alembic import op
from litestar_feedback.support.migration import create_tables, drop_tables


def upgrade() -> None:
    create_tables(op)


def downgrade() -> None:
    drop_tables(op)
```

Then add your row-level security policies on `scope_id` in the same migration.

### Jobs to schedule

The plugin applies timers when an issue is read. These `SupportService` methods are for a
scheduler:

- `sweep_timers()`: apply due timers to every issue.
- `reap_staged()`: delete uploads no message claimed within 24 hours.
- `forget_user(user_id)`: delete a user's screenshots and unlink their issues.
- `import_issue(...)`: insert a historical issue with `notify_skipped_reason='seeded'`.

### Contract test

The package ships the standard's black-box contract as a pytest plugin.

1. Define five fixtures in `tests/conftest.py`: `client`, `customer_headers`,
   `operator_headers`, `other_tenant_headers`, `seeded_issue`.
2. Add `tests/test_support_contract.py`:

```python
from litestar_feedback.support.contract import SupportContract


class TestSupportContract(SupportContract):
    pass
```

Set the rate limit high in the app under test. Every contract test posts as one customer.

## Session replay

`ReplayPlugin` stores rrweb events in chunks, one recording per page load. The browser recorder
is not part of this package: run rrweb in the browser and post its events to the ingest route.

- A session id belongs to the first caller that writes to it. Another scope or user gets 409.
- A repeated `seq` is ignored (`"action": "exists"`), so the browser can retry safely.
- Caps: 1 MiB per chunk, 5,000 events per chunk, 20 MiB per session. Over a cap is 413.
- Playback with `offset_ms` loads from the last full snapshot before that moment.
- Clicks are kept per page path and viewport width bucket, for heatmaps.
- `prune` deletes old recordings. `delete_user` deletes a user's recordings.
- Recordings in the adapter's `protected_sessions` survive both.

### Ingest API

`POST /replay/ingest`, signed in as the user being recorded:

```json
{
  "session_id": "7c0e4b7e-2f1a-4c55-9a51-0d6c1f3f6a10",
  "seq": 0,
  "events": [{"type": 4, "timestamp": 1767225600000, "data": {"href": "https://app.example/orders", "width": 1280, "height": 800}}],
  "entry_url": "https://app.example/orders",
  "viewport_w": 1280,
  "viewport_h": 800
}
```

1. Pick a new `session_id` (UUID) on every page load.
2. Buffer rrweb events. Send them every 10 seconds or 400 events, with `seq` counting from 0.
3. On 5xx, retry the same body. On 4xx, drop the batch.
4. To link a support issue, put `replay_session_id` and `replay_offset_ms` (milliseconds since
   the first event) in the issue's `context`.

The answer is `{"action": "appended" | "exists", "event_count": n}`. 403 means no consent.

### Wire it

```python
from litestar_feedback.replay import ReplayAdapter, ReplayConfig, ReplayPlugin
from litestar_feedback.support import referenced_replay_sessions

replay = ReplayPlugin(
    adapter=ReplayAdapter(
        caller=caller,                     # same functions as for SupportPlugin
        session=session,
        retention_days=30,
        consent=has_analytics_consent,     # async (Caller, Request) -> bool
        protected_sessions=referenced_replay_sessions,  # only if SupportPlugin is installed
    ),
    config=ReplayConfig(path="/v1"),
)
app = Litestar(plugins=[replay])
```

Run the jobs from your scheduler. `system_caller` is an operator `Caller`:

```python
result = await replay.prune(system_caller)            # PruneResult(candidates, deleted, protected)
deleted = await replay.delete_user(system_caller, str(user.id))
```

A runnable version is [examples/replay_app.py](examples/replay_app.py).

### Adapter

| Field | Type | What it does |
|---|---|---|
| `caller` | `async (Request) -> Caller` | Who is calling. Operators can play back and see heatmaps. |
| `session` | `(Caller) -> async context manager of AsyncSession` | One transaction per request. |
| `chunk_store` | `ChunkStore` or None | `async put`, `get`, `delete`. None stores chunks in `replay_chunks.events`. |
| `retention_days` | `int` | `prune` deletes recordings whose last event is older. 0 keeps all. |
| `consent` | `async (Caller, Request) -> bool` or None | False answers 403 and stores nothing. |
| `protected_sessions` | `async (AsyncSession) -> set[UUID]` or None | Recordings that prune and user deletion keep. |

`ReplayConfig` holds `path`, the three caps, `viewport_buckets` (default `0, 640, 1024, 1440`),
the heatmap grid size, `list_limit` and `clock`.

### Routes

| Method | Path | Who |
|---|---|---|
| POST | `/replay/ingest` | the recorded user |
| GET | `/admin/replay/sessions` | operator |
| GET | `/admin/replay/sessions/{id}?offset_ms=` | operator |
| DELETE | `/admin/replay/sessions/{id}` | operator |
| GET | `/admin/replay/heatmaps` | operator |
| GET | `/admin/replay/heatmap?path=&viewport_bucket=` | operator |

A heatmap is a grid: 20 columns across the viewport width, rows of 50 pixels. Click `y` is in
viewport pixels, not page pixels: rrweb reports clicks relative to the viewport.

### Tables

`replay_sessions`, `replay_chunks`, `replay_clicks`. Create them with
`litestar_feedback.replay.migration.create_tables(op)` in an Alembic migration.

## Links

- [CHANGELOG.md](CHANGELOG.md)
- [AGENTS.md](AGENTS.md)
