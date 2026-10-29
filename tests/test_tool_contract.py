"""Tool-level contract: structured results, failures raised as ToolError (not swallowed strings)."""

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server


class RecordingApi:
    """Captures _venmo_api calls and returns canned responses keyed by (method, path)."""

    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def __call__(self, method, path, params=None, body=None):
        self.calls.append({"method": method, "path": path, "body": body})
        return self._responses[(method, path)]


_CREATED = {"data": {"payment": {"id": "999", "status": "settled"}}}


def test_send_money_posts_payment_and_returns_payment_id(monkeypatch):
    api = RecordingApi({("POST", "/payments"): _CREATED})
    monkeypatch.setattr(server, "_venmo_api", api)
    out = server.send_money(
        "42",
        12.5,
        "lunch",
        privacy="friends",
        funding_source_id="fs1",
        assert_friend=False,
    )
    assert api.calls == [
        {
            "method": "POST",
            "path": "/payments",
            "body": {
                "user_id": "42",
                "audience": "friends",
                "amount": 12.5,
                "note": "lunch",
                "funding_source_id": "fs1",
            },
        }
    ]
    assert out["action"] == "sent" and out["amount"] == 12.5 and out["user_id"] == "42"
    assert out["payment_id"] == "999" and out["status"] == "settled"


def test_send_money_uses_default_funding_source(monkeypatch):
    methods = {
        "data": [
            {"id": "bank1", "peer_payment_role": "backup"},
            {"id": "bal1", "peer_payment_role": "default"},
        ]
    }
    api = RecordingApi(
        {("GET", "/payment-methods"): methods, ("POST", "/payments"): _CREATED}
    )
    monkeypatch.setattr(server, "_venmo_api", api)
    server.send_money("42", 1.0, "x", assert_friend=False)
    assert api.calls[-1]["body"]["funding_source_id"] == "bal1"


def test_send_money_without_default_funding_source_sends_nothing(monkeypatch):
    api = RecordingApi({("GET", "/payment-methods"): {"data": []}})
    monkeypatch.setattr(server, "_venmo_api", api)
    with pytest.raises(ToolError, match="No default Venmo funding source"):
        server.send_money("42", 1.0, "x", assert_friend=False)
    assert [c["method"] for c in api.calls] == ["GET"]


def test_send_money_surfaces_200_status_error_code(monkeypatch):
    error = {
        "data": {
            "error_code": 1396,
            "title": "Payment failed",
            "error_msg": "Insufficient funds.",
        }
    }
    api = RecordingApi({("POST", "/payments"): error})
    monkeypatch.setattr(server, "_venmo_api", api)
    with pytest.raises(ToolError, match=r"\(1396\): Insufficient funds"):
        server.send_money("42", 1.0, "x", funding_source_id="fs1", assert_friend=False)


def test_invalid_privacy_rejected_before_any_api_call(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_venmo_api", lambda *a, **k: calls.append(a))
    with pytest.raises(ToolError, match="privacy must be one of"):
        server.send_money("42", 1.0, "x", privacy="everyone")
    assert calls == []


def test_non_numeric_user_id_rejected_before_any_api_call(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_venmo_api", lambda *a, **k: calls.append(a))
    with pytest.raises(ToolError, match="numeric Venmo user ID"):
        server.request_money("maria", 1.0, "x")
    assert calls == []


def test_missing_token_is_a_tool_error(monkeypatch, tmp_path):
    monkeypatch.delenv("VENMO_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(server, "TOKEN_FILE", tmp_path / "missing")
    with pytest.raises(ToolError, match="No Venmo access token"):
        server.get_client()


def test_every_tool_declares_annotations_and_output_schema():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "search_users",
        "send_money",
        "request_money",
        "get_payment_methods",
        "get_friends",
        "get_transactions",
        "get_my_profile",
        "get_pending_requests",
        "complete_request",
        "deny_request",
        "get_outgoing_requests",
        "remind_request",
        "cancel_request",
        "split_request",
        "lookup_user",
    }
    write_tools = {
        "send_money",
        "request_money",
        "complete_request",
        "deny_request",
        "remind_request",
        "cancel_request",
        "split_request",
    }
    for t in tools:
        assert t.annotations is not None, t.name
        assert t.output_schema is not None, t.name
        expected_read_only = t.name not in write_tools
        assert t.annotations.read_only_hint is expected_read_only, t.name


def test_send_money_prefers_configured_funding_source(monkeypatch):
    monkeypatch.setenv("VENMO_FUNDING_SOURCE_ID", "chase1")
    api = RecordingApi({("POST", "/payments"): _CREATED})
    monkeypatch.setattr(server, "_venmo_api", api)
    server.send_money("42", 1.0, "x", assert_friend=False)
    assert [c["method"] for c in api.calls] == ["POST"]  # no payment-methods lookup
    assert api.calls[0]["body"]["funding_source_id"] == "chase1"
