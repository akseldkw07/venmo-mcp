"""Duplicate guard and post-error "was it created?" checks for request_money / send_money."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server

MY_ID = "me"
NOW = datetime.now(timezone.utc)


def _ago(minutes):
    return NOW - timedelta(minutes=minutes)


def _charge(
    cid, *, target="42", amount=285.06, note="rent", status="pending", minutes=1
):
    """A raw /payments charge record I sent (target nested under .user)."""
    return {
        "id": cid,
        "action": "charge",
        "status": status,
        "amount": amount,
        "note": note,
        "date_created": _ago(minutes).strftime("%Y-%m-%dT%H:%M:%S"),
        "actor": {"id": MY_ID},
        "target": {"user": {"id": target}},
    }


class PaymentsApi:
    """GET /payments returns successive charge lists; POST /payments succeeds or raises."""

    def __init__(self, charge_lists, post=None):
        self._lists = list(charge_lists)
        self._post = post
        self.calls = []

    def __call__(self, method, path, params=None, body=None):
        self.calls.append((method, path))
        if method == "GET":
            return {
                "data": self._lists.pop(0) if len(self._lists) > 1 else self._lists[0]
            }
        if isinstance(self._post, Exception):
            raise self._post
        return self._post or {"data": {"payment": {"id": "new", "status": "pending"}}}


def _me(monkeypatch, user_api=None):
    client = SimpleNamespace(
        my_profile=lambda: SimpleNamespace(id=MY_ID), user=user_api
    )
    monkeypatch.setattr(server, "get_client", lambda: client)


# --- request_money -------------------------------------------------------


def test_request_money_refuses_identical_recent_request(monkeypatch):
    _me(monkeypatch)
    api = PaymentsApi([[_charge("old", minutes=3)]])
    monkeypatch.setattr(server, "_venmo_api", api)
    with pytest.raises(ToolError, match="identical request old.*force=True"):
        server.request_money("42", 285.06, "rent", assert_friend=False)
    assert ("POST", "/payments") not in api.calls


def test_request_money_force_sends_despite_duplicate(monkeypatch):
    _me(monkeypatch)
    api = PaymentsApi([[_charge("old", minutes=3)]])
    monkeypatch.setattr(server, "_venmo_api", api)
    out = server.request_money("42", 285.06, "rent", assert_friend=False, force=True)
    assert out["request_id"] == "new"


@pytest.mark.parametrize(
    "other",
    [
        _charge("a", minutes=30),  # outside the window
        _charge("b", amount=100.0),
        _charge("c", note="other"),
        _charge("d", target="43"),
        _charge("e", status="cancelled"),
    ],
)
def test_request_money_ignores_non_matching_requests(monkeypatch, other):
    _me(monkeypatch)
    monkeypatch.setattr(server, "_venmo_api", PaymentsApi([[other]]))
    assert server.request_money("42", 285.06, "rent", assert_friend=False)["request_id"]


def test_request_error_reports_created_true_when_request_exists_anyway(monkeypatch):
    _me(monkeypatch)
    api = PaymentsApi([[], [_charge("ghost")]], post=ToolError("Venmo API error: boom"))
    monkeypatch.setattr(server, "_venmo_api", api)
    with pytest.raises(ToolError, match=r"boom.*created: true.*ghost.*do not retry"):
        server.request_money("42", 285.06, "rent", assert_friend=False)


def test_request_error_reports_created_false_when_nothing_new(monkeypatch):
    _me(monkeypatch)
    # an identical older request exists (sent with force) but nothing new appeared
    api = PaymentsApi([[_charge("old")]], post=ToolError("Venmo API error: boom"))
    monkeypatch.setattr(server, "_venmo_api", api)
    with pytest.raises(ToolError, match=r"boom.*created: false"):
        server.request_money("42", 285.06, "rent", assert_friend=False, force=True)


def test_request_error_reports_unknown_when_check_fails(monkeypatch):
    _me(monkeypatch)
    state = {"n": 0}

    def api(method, path, params=None, body=None):
        if method == "POST":
            raise ToolError("Venmo API error: boom")
        state["n"] += 1
        if state["n"] > 1:
            raise ToolError("Venmo API error: lookup down")
        return {"data": []}

    monkeypatch.setattr(server, "_venmo_api", api)
    with pytest.raises(
        ToolError, match=r"boom.*created: unknown.*get_outgoing_requests"
    ):
        server.request_money("42", 285.06, "rent", assert_friend=False)


# --- send_money ----------------------------------------------------------


def _sent(tid, *, amount=0.47, note="lunch", minutes=1, actor=MY_ID, kind="pay"):
    return SimpleNamespace(
        id=tid,
        payment_type=kind,
        amount=amount,
        note=note,
        status="settled",
        date_created=str(int(_ago(minutes).timestamp())),
        actor=SimpleNamespace(id=actor),
    )


class FeedApi:
    """get_transaction_between_two_users returns successive feeds."""

    def __init__(self, feeds):
        self._feeds = list(feeds)

    def get_transaction_between_two_users(self, **kwargs):
        return self._feeds.pop(0) if len(self._feeds) > 1 else self._feeds[0]


def test_send_money_refuses_identical_recent_payment(monkeypatch):
    _me(monkeypatch, FeedApi([[_sent("t1", minutes=2)]]))
    calls = []
    monkeypatch.setattr(server, "_venmo_api", lambda *a, **k: calls.append(a))
    with pytest.raises(ToolError, match="identical payment t1.*force=True"):
        server.send_money(
            "42", 0.47, "lunch", funding_source_id="fs", assert_friend=False
        )
    assert calls == []


def test_send_money_ignores_payments_received_from_them(monkeypatch):
    _me(monkeypatch, FeedApi([[_sent("t1", actor="42")]]))
    monkeypatch.setattr(
        server, "_venmo_api", lambda *a, **k: {"data": {"payment": {"id": "p"}}}
    )
    out = server.send_money(
        "42", 0.47, "lunch", funding_source_id="fs", assert_friend=False
    )
    assert out["payment_id"] == "p"


def test_send_error_reports_created_true(monkeypatch):
    _me(monkeypatch, FeedApi([[], [_sent("t9")]]))

    def boom(*a, **k):
        raise ToolError("Venmo API error: boom")

    monkeypatch.setattr(server, "_venmo_api", boom)
    with pytest.raises(ToolError, match=r"boom.*created: true.*t9.*do not retry"):
        server.send_money(
            "42", 0.47, "lunch", funding_source_id="fs", assert_friend=False
        )
