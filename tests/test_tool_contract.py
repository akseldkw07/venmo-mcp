"""Tool-level contract: structured results, failures raised as ToolError (not swallowed strings)."""

import asyncio
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server


class FakePaymentApi:
    def __init__(self, ok):
        self._ok = ok
        self.calls = []

    def send_money(self, **kwargs):
        self.calls.append(("send_money", kwargs))
        return self._ok

    def request_money(self, **kwargs):
        self.calls.append(("request_money", kwargs))
        return self._ok


def _client_with_payment(monkeypatch, ok):
    client = SimpleNamespace(payment=FakePaymentApi(ok))
    monkeypatch.setattr(server, "get_client", lambda: client)
    return client


def test_send_money_success_returns_structured_result(monkeypatch):
    client = _client_with_payment(monkeypatch, ok=True)
    out = server.send_money(
        "42", 12.5, "lunch", privacy="friends", funding_source_id="fs1"
    )
    assert out["action"] == "sent" and out["amount"] == 12.5 and out["user_id"] == "42"
    _, kwargs = client.payment.calls[0]
    assert kwargs["target_user_id"] == 42
    assert kwargs["funding_source_id"] == "fs1"
    assert kwargs["privacy_setting"] == server.PaymentPrivacy.FRIENDS


def test_send_money_failure_raises_tool_error(monkeypatch):
    _client_with_payment(monkeypatch, ok=False)
    with pytest.raises(ToolError, match="Payment failed"):
        server.send_money("42", 1.0, "x")


def test_request_money_failure_raises_tool_error(monkeypatch):
    _client_with_payment(monkeypatch, ok=False)
    with pytest.raises(ToolError, match="Request failed"):
        server.request_money("42", 1.0, "x")


def test_invalid_privacy_rejected_before_any_api_call(monkeypatch):
    client = _client_with_payment(monkeypatch, ok=True)
    with pytest.raises(ToolError, match="privacy must be one of"):
        server.send_money("42", 1.0, "x", privacy="everyone")
    assert client.payment.calls == []


def test_non_numeric_user_id_rejected_before_any_api_call(monkeypatch):
    client = _client_with_payment(monkeypatch, ok=True)
    with pytest.raises(ToolError, match="numeric Venmo user ID"):
        server.request_money("maria", 1.0, "x")
    assert client.payment.calls == []


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
    }
    write_tools = {"send_money", "request_money", "complete_request", "deny_request"}
    for t in tools:
        assert t.annotations is not None, t.name
        assert t.output_schema is not None, t.name
        expected_read_only = t.name not in write_tools
        assert t.annotations.read_only_hint is expected_read_only, t.name
