"""Slow writes return {status: pending, lookup_id} before the bridge's 60s cutoff."""

import threading

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server


@pytest.fixture(autouse=True)
def _fast_budget(monkeypatch):
    monkeypatch.setattr(server, "WRITE_TIME_BUDGET", 0.2)
    monkeypatch.setattr(server, "_recent_requests", lambda *a: [])
    monkeypatch.setattr(server, "_recent_sends", lambda *a: [])


def _slow_post(release, *, fail=False):
    def api(method, path, params=None, body=None):
        release.wait(5)
        if fail:
            raise ToolError("Venmo API error: blocked")
        return {"data": {"payment": {"id": "slow1", "status": "pending"}}}

    return api


def test_fast_request_returns_normally(monkeypatch):
    release = threading.Event()
    release.set()
    monkeypatch.setattr(server, "_venmo_api", _slow_post(release))
    out = server.request_money("42", 1.0, "x", assert_friend=False)
    assert out["request_id"] == "slow1" and out["lookup_id"] is None


def test_slow_request_returns_pending_then_status_resolves(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(server, "_venmo_api", _slow_post(release))
    out = server.request_money("42", 1.0, "x", assert_friend=False)
    assert out["status"] == "pending" and out["request_id"] is None
    lookup_id = out["lookup_id"]
    assert lookup_id
    assert server.get_write_status(lookup_id)["state"] == "running"

    release.set()
    server._PENDING_WRITES[lookup_id].result(timeout=5)
    status = server.get_write_status(lookup_id)
    assert status["state"] == "done"
    assert status["result"]["request_id"] == "slow1"


def test_slow_send_that_fails_reports_the_error(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(server, "_venmo_api", _slow_post(release, fail=True))
    out = server.send_money("42", 1.0, "x", funding_source_id="fs", assert_friend=False)
    assert out["status"] == "pending" and out["payment_id"] is None
    release.set()
    with pytest.raises(ToolError):
        server._PENDING_WRITES[out["lookup_id"]].result(timeout=5)
    status = server.get_write_status(out["lookup_id"])
    assert status["state"] == "failed"
    assert "blocked" in status["error"] and "created: false" in status["error"]


def test_validation_errors_still_raise_immediately(monkeypatch):
    with pytest.raises(ToolError, match="numeric Venmo user ID"):
        server.request_money("maria", 1.0, "x")


def test_unknown_lookup_id_is_a_tool_error():
    with pytest.raises(ToolError, match="Unknown lookup_id"):
        server.get_write_status("nope")
