"""Black-box contract test of the support HTTP surface (ISSUES-STANDARD.md, "Proof").

It uses only HTTP and the five fixtures from ``pytest_plugin``. It imports nothing from the
product. Subclass ``SupportContract`` in a ``test_*.py`` file to run it.

The app under test needs a rate limit high enough for about 80 customer messages: every test
posts as the same customer.

Upload tests skip when ``POST /issue-attachments`` answers 503 (no store configured).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

Json = dict[str, Any]
Headers = dict[str, str]


def _open(client: Any, headers: Headers, body: str = "The export button does nothing") -> Json:
    res = client.post("/issues", json={"kind": "bug", "body": body}, headers=headers)
    assert res.status_code == 201, res.text
    return res.json()


def _customer_view(client: Any, issue_id: str, headers: Headers) -> Json:
    res = client.get(f"/issues/{issue_id}", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def _operator_view(client: Any, issue_id: str, headers: Headers) -> Json:
    res = client.get(f"/admin/issues/{issue_id}", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def _last_event_id(issue: Json) -> str:
    return issue["events"][-1]["id"]


def _set_status(client: Any, issue_id: str, to: str, headers: Headers, **extra: Any) -> Any:
    last = _last_event_id(_operator_view(client, issue_id, headers))
    return client.post(
        f"/admin/issues/{issue_id}/status",
        json={"to": to, "last_seen_event_id": last, **extra},
        headers=headers,
    )


def _upload(client: Any, headers: Headers) -> Json:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    res = client.post(
        "/issue-attachments", files={"file": ("shot.png", png, "image/png")}, headers=headers
    )
    if res.status_code == 503:
        pytest.skip("no attachment store configured")
    assert res.status_code == 201, res.text
    return res.json()


class SupportContract:
    """The contract. pytest collects it through a ``Test*`` subclass in your repo."""

    # -- opening and reading ------------------------------------------------------------

    def test_customer_opens_issue(self, client: Any, customer_headers: Headers) -> None:
        issue = _open(client, customer_headers)
        assert issue["status"] == "open"
        assert issue["kind"] == "bug"
        assert issue["events"][0]["event_type"] == "opened"

    def test_title_defaults_to_first_line(self, client: Any, customer_headers: Headers) -> None:
        issue = _open(client, customer_headers, body="First line\nmore detail")
        assert issue["title"] == "First line"

    def test_kind_is_optional(self, client: Any, customer_headers: Headers) -> None:
        res = client.post("/issues", json={"body": "no kind"}, headers=customer_headers)
        assert res.status_code == 201, res.text
        assert res.json()["kind"] is None

    def test_empty_body_is_rejected(self, client: Any, customer_headers: Headers) -> None:
        res = client.post("/issues", json={"body": ""}, headers=customer_headers)
        assert res.status_code in {400, 422}

    def test_unknown_kind_is_rejected(self, client: Any, customer_headers: Headers) -> None:
        res = client.post("/issues", json={"body": "x", "kind": "rant"}, headers=customer_headers)
        assert res.status_code in {400, 422}

    def test_list_contains_new_issue(self, client: Any, customer_headers: Headers) -> None:
        issue = _open(client, customer_headers)
        res = client.get("/issues", headers=customer_headers)
        assert res.status_code == 200
        assert issue["id"] in [i["id"] for i in res.json()]

    def test_seeded_issue_is_readable(
        self, client: Any, customer_headers: Headers, seeded_issue: Json
    ) -> None:
        data = _customer_view(client, seeded_issue["id"], customer_headers)
        assert data["id"] == seeded_issue["id"]

    def test_dedupe_key_returns_the_live_issue(
        self, client: Any, customer_headers: Headers
    ) -> None:
        key = f"probe-{uuid.uuid4()}"
        first = client.post(
            "/issues", json={"body": "disk full", "dedupe_key": key}, headers=customer_headers
        )
        second = client.post(
            "/issues", json={"body": "disk full", "dedupe_key": key}, headers=customer_headers
        )
        assert first.status_code == 201, first.text
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first.json()["id"]

    def test_dedupe_key_opens_new_issue_after_close(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        key = f"probe-{uuid.uuid4()}"
        first = client.post(
            "/issues", json={"body": "disk full", "dedupe_key": key}, headers=customer_headers
        ).json()
        assert _set_status(client, first["id"], "closed", operator_headers).status_code == 200
        again = client.post(
            "/issues", json={"body": "disk full", "dedupe_key": key}, headers=customer_headers
        )
        assert again.status_code == 201, again.text
        assert again.json()["id"] != first["id"]

    # -- replies ------------------------------------------------------------------------

    def test_operator_reply_reaches_customer(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        res = client.post(
            f"/admin/issues/{issue['id']}/comments",
            json={"body": "We are on it."},
            headers=operator_headers,
        )
        assert res.status_code == 201, res.text
        data = _customer_view(client, issue["id"], customer_headers)
        assert data["has_unread_reply"] is True
        assert data["status"] == "waiting_customer"
        comments = [e for e in data["events"] if e["event_type"] == "comment"]
        assert [e["body"] for e in comments] == ["We are on it."]

    def test_unread_flag_on_list(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        client.post(
            f"/admin/issues/{issue['id']}/comments", json={"body": "hi"}, headers=operator_headers
        )
        rows = client.get("/issues", headers=customer_headers).json()
        assert next(r for r in rows if r["id"] == issue["id"])["has_unread_reply"] is True

    def test_customer_reply_clears_unread_and_reopens(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        client.post(
            f"/admin/issues/{issue['id']}/comments", json={"body": "?"}, headers=operator_headers
        )
        res = client.post(
            f"/issues/{issue['id']}/comments", json={"body": "here"}, headers=customer_headers
        )
        assert res.status_code == 201, res.text
        data = _customer_view(client, issue["id"], customer_headers)
        assert data["status"] == "open"
        assert data["has_unread_reply"] is False

    def test_internal_note_hidden_from_customer(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        res = client.post(
            f"/admin/issues/{issue['id']}/comments",
            json={"body": "Secret note", "internal": True},
            headers=operator_headers,
        )
        assert res.status_code == 201, res.text
        data = _customer_view(client, issue["id"], customer_headers)
        assert all(e["body"] != "Secret note" for e in data["events"])
        assert all(not e["internal"] for e in data["events"])
        assert data["status"] == "open"
        assert data["has_unread_reply"] is False

    def test_operator_sees_internal_note(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        client.post(
            f"/admin/issues/{issue['id']}/comments",
            json={"body": "Internal only", "internal": True},
            headers=operator_headers,
        )
        data = _operator_view(client, issue["id"], operator_headers)
        assert [e["body"] for e in data["events"] if e["internal"]] == ["Internal only"]

    def test_customer_reply_on_closed_is_409(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        assert _set_status(client, issue["id"], "closed", operator_headers).status_code == 200
        res = client.post(
            f"/issues/{issue['id']}/comments", json={"body": "hello?"}, headers=customer_headers
        )
        assert res.status_code == 409

    def test_operator_reply_on_closed_is_409(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        assert _set_status(client, issue["id"], "closed", operator_headers).status_code == 200
        res = client.post(
            f"/admin/issues/{issue['id']}/comments",
            json={"body": "too late"},
            headers=operator_headers,
        )
        assert res.status_code == 409

    def test_reply_on_resolved_reopens(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        assert _set_status(client, issue["id"], "resolved", operator_headers).status_code == 200
        res = client.post(
            f"/issues/{issue['id']}/comments",
            json={"body": "still broken"},
            headers=customer_headers,
        )
        assert res.status_code == 201, res.text
        assert _customer_view(client, issue["id"], customer_headers)["status"] == "open"

    # -- customer resolve ---------------------------------------------------------------

    def test_customer_resolves(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        client.post(
            f"/admin/issues/{issue['id']}/comments",
            json={"body": "Fixed."},
            headers=operator_headers,
        )
        last = _last_event_id(_customer_view(client, issue["id"], customer_headers))
        res = client.post(
            f"/issues/{issue['id']}/resolve",
            json={"last_seen_event_id": last},
            headers=customer_headers,
        )
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "resolved"

    def test_resolve_with_stale_view_is_409(self, client: Any, customer_headers: Headers) -> None:
        issue = _open(client, customer_headers)
        res = client.post(
            f"/issues/{issue['id']}/resolve",
            json={"last_seen_event_id": str(uuid.uuid4())},
            headers=customer_headers,
        )
        assert res.status_code == 409

    def test_resolve_needs_last_seen_event_id(self, client: Any, customer_headers: Headers) -> None:
        issue = _open(client, customer_headers)
        res = client.post(f"/issues/{issue['id']}/resolve", json={}, headers=customer_headers)
        assert res.status_code in {400, 422}

    # -- operator status ----------------------------------------------------------------

    @pytest.mark.parametrize(
        "path",
        [
            ["waiting_customer", "open"],
            ["waiting_customer", "resolved"],
            ["waiting_customer", "closed"],
            ["resolved", "open"],
            ["resolved", "closed"],
            ["closed"],
        ],
    )
    def test_allowed_transitions(
        self,
        client: Any,
        customer_headers: Headers,
        operator_headers: Headers,
        path: list[str],
    ) -> None:
        issue = _open(client, customer_headers)
        for to in path:
            res = _set_status(client, issue["id"], to, operator_headers)
            assert res.status_code == 200, f"move to {to}: {res.text}"
            assert res.json()["status"] == to

    @pytest.mark.parametrize(
        ("path", "bad"),
        [
            (["closed"], "open"),
            (["closed"], "resolved"),
            (["closed"], "waiting_customer"),
            (["resolved"], "waiting_customer"),
            ([], "open"),
        ],
    )
    def test_invalid_transitions_are_409(
        self,
        client: Any,
        customer_headers: Headers,
        operator_headers: Headers,
        path: list[str],
        bad: str,
    ) -> None:
        issue = _open(client, customer_headers)
        for to in path:
            assert _set_status(client, issue["id"], to, operator_headers).status_code == 200
        assert _set_status(client, issue["id"], bad, operator_headers).status_code == 409

    def test_status_change_writes_event(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        _set_status(client, issue["id"], "waiting_customer", operator_headers)
        data = _operator_view(client, issue["id"], operator_headers)
        changes = [e for e in data["events"] if e["event_type"] == "status_change"]
        assert [e["body"] for e in changes] == ["waiting_customer"]

    def test_close_reason_is_an_internal_note(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        res = _set_status(client, issue["id"], "closed", operator_headers, reason="duplicate")
        assert res.status_code == 200, res.text
        op = _operator_view(client, issue["id"], operator_headers)
        assert any(e["internal"] and e["body"] == "duplicate" for e in op["events"])
        cust = _customer_view(client, issue["id"], customer_headers)
        assert all(e["body"] != "duplicate" for e in cust["events"])

    def test_status_with_stale_view_is_409(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        res = client.post(
            f"/admin/issues/{issue['id']}/status",
            json={"to": "closed", "last_seen_event_id": str(uuid.uuid4())},
            headers=operator_headers,
        )
        assert res.status_code == 409

    # -- access -------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/admin/issues"),
            ("get", "/admin/issues/{id}"),
            ("post", "/admin/issues/{id}/comments"),
            ("post", "/admin/issues/{id}/status"),
        ],
    )
    def test_customer_cannot_use_admin_routes(
        self, client: Any, customer_headers: Headers, method: str, path: str
    ) -> None:
        issue = _open(client, customer_headers)
        url = path.format(id=issue["id"])
        body = {"body": "x", "to": "closed", "last_seen_event_id": str(uuid.uuid4())}
        if method == "get":
            res = client.get(url, headers=customer_headers)
        else:
            res = client.post(url, json=body, headers=customer_headers)
        assert res.status_code in {401, 403}

    def test_other_tenant_cannot_read(
        self, client: Any, customer_headers: Headers, other_tenant_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        assert client.get(f"/issues/{issue['id']}", headers=other_tenant_headers).status_code == 404

    def test_other_tenant_cannot_comment(
        self, client: Any, customer_headers: Headers, other_tenant_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        res = client.post(
            f"/issues/{issue['id']}/comments", json={"body": "sneaky"}, headers=other_tenant_headers
        )
        assert res.status_code == 404

    def test_other_tenant_cannot_resolve(
        self, client: Any, customer_headers: Headers, other_tenant_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        res = client.post(
            f"/issues/{issue['id']}/resolve",
            json={"last_seen_event_id": _last_event_id(issue)},
            headers=other_tenant_headers,
        )
        assert res.status_code == 404

    def test_other_tenant_list_excludes_issue(
        self, client: Any, customer_headers: Headers, other_tenant_headers: Headers
    ) -> None:
        issue = _open(client, customer_headers)
        rows = client.get("/issues", headers=other_tenant_headers).json()
        assert issue["id"] not in [i["id"] for i in rows]

    def test_operator_lists_every_scope(
        self,
        client: Any,
        customer_headers: Headers,
        other_tenant_headers: Headers,
        operator_headers: Headers,
    ) -> None:
        a = _open(client, customer_headers)
        b = _open(client, other_tenant_headers)
        ids = [
            i["id"] for i in client.get("/admin/issues?limit=200", headers=operator_headers).json()
        ]
        assert a["id"] in ids
        assert b["id"] in ids

    def test_replay_context_hidden_from_customer(
        self, client: Any, customer_headers: Headers, operator_headers: Headers
    ) -> None:
        context = {
            "page_url": "/orders",
            "replay_session_id": str(uuid.uuid4()),
            "replay_offset_ms": 5,
        }
        res = client.post(
            "/issues", json={"body": "with replay", "context": context}, headers=customer_headers
        )
        assert res.status_code == 201, res.text
        cust = _customer_view(client, res.json()["id"], customer_headers)
        assert cust["context"] == {"page_url": "/orders"}
        op = _operator_view(client, res.json()["id"], operator_headers)
        assert op["context"] == context

    # -- attachments --------------------------------------------------------------------

    def test_upload_then_attach(self, client: Any, customer_headers: Headers) -> None:
        att = _upload(client, customer_headers)
        res = client.post(
            "/issues",
            json={"body": "see screenshot", "attachment_ids": [att["id"]]},
            headers=customer_headers,
        )
        assert res.status_code == 201, res.text
        assert [a["id"] for a in res.json()["events"][0]["attachments"]] == [att["id"]]

    def test_attachment_is_single_use(self, client: Any, customer_headers: Headers) -> None:
        att = _upload(client, customer_headers)
        issue = client.post(
            "/issues", json={"body": "one", "attachment_ids": [att["id"]]}, headers=customer_headers
        ).json()
        res = client.post(
            f"/issues/{issue['id']}/comments",
            json={"body": "again", "attachment_ids": [att["id"]]},
            headers=customer_headers,
        )
        assert res.status_code == 422

    def test_other_tenant_cannot_attach_upload(
        self, client: Any, customer_headers: Headers, other_tenant_headers: Headers
    ) -> None:
        att = _upload(client, customer_headers)
        res = client.post(
            "/issues",
            json={"body": "stolen", "attachment_ids": [att["id"]]},
            headers=other_tenant_headers,
        )
        assert res.status_code == 422

    def test_non_image_upload_is_rejected(self, client: Any, customer_headers: Headers) -> None:
        _upload(client, customer_headers)
        res = client.post(
            "/issue-attachments",
            files={"file": ("x.txt", b"hello", "text/plain")},
            headers=customer_headers,
        )
        assert res.status_code == 422
