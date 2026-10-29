"""Library and network failures reach the caller as ToolErrors with their reason, not a bare crash."""

from types import SimpleNamespace

import pytest
import requests
from mcp.server.mcpserver.exceptions import ToolError
from venmo_api.models.exception import ResourceNotFoundError

import server


def test_network_failure_is_a_tool_error(monkeypatch):
    monkeypatch.setenv("VENMO_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(
        server,
        "get_client",
        lambda: SimpleNamespace(my_profile=lambda: SimpleNamespace(id="me")),
    )

    def timeout(*a, **k):
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(server.requests, "request", timeout)
    with pytest.raises(
        ToolError, match="Could not reach Venmo.*Timeout.*read timed out"
    ):
        server.get_pending_requests()


def test_library_error_in_tool_is_a_tool_error(monkeypatch):
    def boom():
        raise ResourceNotFoundError(msg="Venmo said no")

    monkeypatch.setattr(server, "get_client", boom)
    with pytest.raises(ToolError, match="ResourceNotFoundError.*Venmo said no"):
        server.get_friends()


def test_library_error_inside_friend_check_is_a_tool_error(monkeypatch):
    def boom():
        raise ResourceNotFoundError(msg="profile lookup failed")

    monkeypatch.setattr(server, "get_client", boom)
    with pytest.raises(ToolError, match="profile lookup failed"):
        server.request_money("42", 1.0, "x")


def test_unrelated_bugs_still_crash(monkeypatch):
    monkeypatch.setattr(
        server, "get_client", lambda: SimpleNamespace()
    )  # no my_profile -> AttributeError
    with pytest.raises(AttributeError):
        server.get_friends()
