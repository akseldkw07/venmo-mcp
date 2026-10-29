"""assert_friend: money tools refuse non-friend counterparties before touching /payments."""

from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server
from conftest import RecordingApi

pytestmark = pytest.mark.usefixtures("no_recent_duplicates")


FRIENDS = {"42", "43"}


def _patch_friends(monkeypatch, friends=FRIENDS):
    monkeypatch.setattr(server, "_friend_ids", lambda: set(friends))


def _fail_on_api(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_venmo_api", lambda *a, **k: calls.append(a))
    return calls


def test_friend_ids_pages_until_empty(monkeypatch):
    monkeypatch.setattr(
        server,
        "get_client",
        lambda: SimpleNamespace(my_profile=lambda: SimpleNamespace(id="me")),
    )
    monkeypatch.setattr(server, "_FRIENDS_PAGE_SIZE", 2)
    pages = {0: [{"id": "1"}, {"id": "2"}], 2: [{"id": "3"}], 3: []}
    api = RecordingApi(
        {("GET", "/users/me/friends"): lambda p: {"data": pages[p["offset"]]}}
    )
    monkeypatch.setattr(server, "_venmo_api", api)
    assert server._friend_ids() == {"1", "2", "3"}
    assert [c["params"]["offset"] for c in api.calls] == [0, 2, 3]


def test_send_money_refuses_non_friend(monkeypatch):
    _patch_friends(monkeypatch)
    calls = _fail_on_api(monkeypatch)
    with pytest.raises(ToolError, match="not one of your Venmo friends"):
        server.send_money("99", 0.01, "x", funding_source_id="fs1")
    assert calls == []


def test_request_money_refuses_non_friend(monkeypatch):
    _patch_friends(monkeypatch)
    calls = _fail_on_api(monkeypatch)
    with pytest.raises(ToolError, match="99"):
        server.request_money("99", 0.01, "x")
    assert calls == []


def test_request_money_allows_friend(monkeypatch):
    _patch_friends(monkeypatch)
    api = RecordingApi({("POST", "/payments"): {"data": {"payment": {"id": "7"}}}})
    monkeypatch.setattr(server, "_venmo_api", api)
    assert server.request_money("42", 0.01, "x")["request_id"] == "7"


def test_assert_friend_false_skips_the_check(monkeypatch):
    monkeypatch.setattr(
        server, "_friend_ids", lambda: pytest.fail("friend check should be skipped")
    )
    api = RecordingApi({("POST", "/payments"): {"data": {"payment": {"id": "7"}}}})
    monkeypatch.setattr(server, "_venmo_api", api)
    assert (
        server.request_money("99", 0.01, "x", assert_friend=False)["request_id"] == "7"
    )


def test_split_request_refuses_if_any_user_is_not_a_friend(monkeypatch):
    _patch_friends(monkeypatch)
    calls = _fail_on_api(monkeypatch)
    with pytest.raises(ToolError, match="99"):
        server.split_request(["42", "99", "43"], 3.0, "x")
    assert calls == []  # nobody is charged when any user fails the check


def test_complete_request_refuses_when_requester_is_not_a_friend(monkeypatch):
    _patch_friends(monkeypatch)
    api = RecordingApi(
        {("GET", "/payments/5"): {"data": {"id": "5", "actor": {"id": "99"}}}}
    )
    monkeypatch.setattr(server, "_venmo_api", api)
    with pytest.raises(ToolError, match="99"):
        server.complete_request("5")
    assert [c["method"] for c in api.calls] == ["GET"]  # never PUT approve
