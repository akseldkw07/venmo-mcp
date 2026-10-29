"""Tests for viewing and completing pending (incomplete) requests made to me.

These exercise the raw-REST seam (`_venmo_api`) and the new tools without touching
the network or moving money: the API layer is patched per test.
"""

from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server


def _fake_response(body, status_code=200):
    """A stand-in for a requests.Response whose .json() returns body (or raises it)."""

    def _json():
        if isinstance(body, Exception):
            raise body
        return body

    return SimpleNamespace(status_code=status_code, json=_json)


def test_payment_id_accepts_numeric_string():
    assert server._payment_id("4716790057747324831") == "4716790057747324831"


def test_payment_id_rejects_non_numeric():
    with pytest.raises(ToolError, match="numeric Venmo request ID"):
        server._payment_id("abc")


def test_venmo_api_returns_parsed_body(monkeypatch):
    monkeypatch.setenv("VENMO_ACCESS_TOKEN", "tok")
    captured = {}

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        captured.update(
            method=method, url=url, headers=headers, params=params, json=json
        )
        return _fake_response({"data": [{"id": "1"}]})

    monkeypatch.setattr(server.requests, "request", fake_request)
    out = server._venmo_api("GET", "/payments", params={"action": "charge"})
    assert out == {"data": [{"id": "1"}]}
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.venmo.com/v1/payments"
    assert captured["headers"]["Authorization"] == "Bearer tok"


def test_venmo_api_raises_on_error_payload(monkeypatch):
    monkeypatch.setenv("VENMO_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(
        server.requests,
        "request",
        lambda *a, **k: _fake_response({"error": {"message": "No pending payment."}}),
    )
    with pytest.raises(ToolError, match="No pending payment"):
        server._venmo_api("PUT", "/payments/1", body={"action": "approve"})


def test_venmo_api_raises_on_non_json(monkeypatch):
    monkeypatch.setenv("VENMO_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(
        server.requests,
        "request",
        lambda *a, **k: _fake_response(ValueError("not json"), status_code=502),
    )
    with pytest.raises(ToolError, match="non-JSON"):
        server._venmo_api("GET", "/payments")


def _charge(
    payment_id, status, target_id, *, actor_user="payer", amount=10.0, note="n"
):
    """A /payments charge record as Venmo returns it (target nested under .user)."""
    return {
        "id": payment_id,
        "action": "charge",
        "status": status,
        "amount": amount,
        "note": note,
        "date_created": "2026-10-26T22:19:51",
        "actor": {"id": "99", "username": actor_user, "display_name": "Payer"},
        "target": {"user": {"id": target_id, "username": "me", "display_name": "Me"}},
    }


def _patch_identity(monkeypatch, my_id="me"):
    monkeypatch.setattr(
        server,
        "get_client",
        lambda: SimpleNamespace(my_profile=lambda: SimpleNamespace(id=my_id)),
    )


def test_get_pending_requests_returns_only_pending_charges_to_me(monkeypatch):
    _patch_identity(monkeypatch)
    records = {
        "data": [
            _charge(
                "1", "pending", "me", actor_user="alice", amount=100.0, note="Tables"
            ),
            _charge("2", "settled", "me"),  # already paid -> excluded
            _charge("3", "pending", "someone-else"),  # I'm the requester -> excluded
        ]
    }
    monkeypatch.setattr(server, "_venmo_api", lambda *a, **k: records)
    out = server.get_pending_requests()
    assert [r["id"] for r in out] == ["1"]
    assert out[0] == {
        "id": "1",
        "amount": 100.0,
        "note": "Tables",
        "date": "2026-10-26T22:19:51",
        "status": "pending",
        "from_user": {"id": "99", "username": "alice", "display_name": "Payer"},
    }


def test_get_pending_requests_respects_limit(monkeypatch):
    _patch_identity(monkeypatch)
    records = {"data": [_charge(str(i), "pending", "me") for i in range(10)]}
    monkeypatch.setattr(server, "_venmo_api", lambda *a, **k: records)
    assert len(server.get_pending_requests(limit=3)) == 3


class RecordingApi:
    """Captures _venmo_api calls and returns a canned response."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    def __call__(self, method, path, params=None, body=None):
        self.calls.append({"method": method, "path": path, "body": body})
        return self._response


def test_complete_request_approves_and_returns_result(monkeypatch):
    api = RecordingApi({"data": {"id": "1", "status": "settled", "amount": 100.0}})
    monkeypatch.setattr(server, "_venmo_api", api)
    out = server.complete_request("1")
    assert api.calls == [
        {"method": "PUT", "path": "/payments/1", "body": {"action": "approve"}}
    ]
    assert out["action"] == "completed"
    assert out["request_id"] == "1"
    assert out["status"] == "settled"


def test_complete_request_rejects_non_numeric_before_api(monkeypatch):
    api = RecordingApi({"data": {}})
    monkeypatch.setattr(server, "_venmo_api", api)
    with pytest.raises(ToolError, match="numeric Venmo request ID"):
        server.complete_request("not-an-id")
    assert api.calls == []


def test_deny_request_denies(monkeypatch):
    api = RecordingApi({"data": {"id": "1", "status": "denied"}})
    monkeypatch.setattr(server, "_venmo_api", api)
    out = server.deny_request("1")
    assert api.calls[0]["body"] == {"action": "deny"}
    assert out["action"] == "denied"
    assert out["request_id"] == "1"
