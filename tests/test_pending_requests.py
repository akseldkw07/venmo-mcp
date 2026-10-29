"""Tests for viewing and completing pending (incomplete) requests made to me.

These exercise the raw-REST seam (`_venmo_api`) and the new tools without touching
the network or moving money: the API layer is patched per test.
"""

from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server


@pytest.fixture(autouse=True)
def _no_recent_duplicates(monkeypatch):
    """These tests aren't about the duplicate guard: report no recent matches."""
    monkeypatch.setattr(server, "_recent_requests", lambda *a: [])
    monkeypatch.setattr(server, "_recent_sends", lambda *a: [])


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
    out = server.complete_request("1", assert_friend=False)
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


# --- get_outgoing_requests -----------------------------------------------


def _payment(payment_id, status, *, target_user="debtor", amount=10.0, note="n"):
    """A charge Payment as the venmo_api client returns it (actor is always me)."""
    return SimpleNamespace(
        id=payment_id,
        status=status,  # real client gives a PaymentStatus enum; a str works too
        amount=amount,
        note=note,
        date_created=1780887063,
        actor=SimpleNamespace(id="me", username="me", display_name="Me"),
        target=SimpleNamespace(id="7", username=target_user, display_name="Debtor"),
    )


def _patch_payment_api(monkeypatch, charges):
    client = SimpleNamespace(
        payment=SimpleNamespace(get_charge_payments=lambda limit=100: list(charges)),
        my_profile=lambda: SimpleNamespace(id="me"),
    )
    monkeypatch.setattr(server, "get_client", lambda: client)
    return client


def test_get_outgoing_requests_returns_only_pending_charges_i_sent(monkeypatch):
    _patch_payment_api(
        monkeypatch,
        [
            _payment("1", "pending", target_user="alice", amount=25.0, note="dinner"),
            _payment("2", "settled", target_user="bob"),  # already paid -> excluded
        ],
    )
    out = server.get_outgoing_requests()
    assert [r["id"] for r in out] == ["1"]
    assert out[0]["amount"] == 25.0
    assert out[0]["note"] == "dinner"
    assert out[0]["status"] == "pending"
    assert out[0]["to_user"] == {
        "id": "7",
        "username": "alice",
        "display_name": "Debtor",
    }


# --- remind_request / cancel_request -------------------------------------


class RecordingPaymentApi:
    """Records remind/cancel calls; returns `result` or raises `raises`."""

    def __init__(self, result=True, raises=None):
        self._result = result
        self._raises = raises
        self.calls = []

    def remind_payment(self, payment=None, payment_id=None):
        self.calls.append(("remind", payment_id))
        if self._raises:
            raise self._raises
        return self._result

    def cancel_payment(self, payment=None, payment_id=None):
        self.calls.append(("cancel", payment_id))
        if self._raises:
            raise self._raises
        return self._result


def _patch_payment(monkeypatch, api):
    monkeypatch.setattr(server, "get_client", lambda: SimpleNamespace(payment=api))
    return api


def test_remind_request_calls_remind_and_returns_result(monkeypatch):
    api = _patch_payment(monkeypatch, RecordingPaymentApi(result=True))
    out = server.remind_request("4716790057747324831")
    assert api.calls == [("remind", "4716790057747324831")]
    assert out["action"] == "reminded"
    assert out["request_id"] == "4716790057747324831"


def test_remind_request_wraps_library_error(monkeypatch):
    _patch_payment(
        monkeypatch, RecordingPaymentApi(raises=RuntimeError("already reminded"))
    )
    with pytest.raises(ToolError, match="already reminded"):
        server.remind_request("1")


def test_cancel_request_calls_cancel_and_returns_result(monkeypatch):
    api = _patch_payment(monkeypatch, RecordingPaymentApi(result=True))
    out = server.cancel_request("4716790057747324831")
    assert api.calls == [("cancel", "4716790057747324831")]
    assert out["action"] == "cancelled"
    assert out["request_id"] == "4716790057747324831"


# --- split_request -------------------------------------------------------


class RecordingRequestApi:
    def __init__(self):
        self.calls = []

    def request_money(self, **kwargs):
        self.calls.append(kwargs)
        return True


def test_split_request_splits_evenly_to_the_cent(monkeypatch):
    api = RecordingRequestApi()
    monkeypatch.setattr(server, "get_client", lambda: SimpleNamespace(payment=api))
    out = server.split_request(["1", "2", "3"], 10.0, "dinner", assert_friend=False)
    # 10.00 / 3 -> 3.34, 3.33, 3.33 (remainder cent goes to the first)
    amounts = [c["amount"] for c in api.calls]
    assert amounts == [3.34, 3.33, 3.33]
    assert round(sum(amounts), 2) == 10.0
    assert [c["target_user_id"] for c in api.calls] == [1, 2, 3]
    assert all(c["note"] == "dinner" for c in api.calls)
    assert out["count"] == 3
    assert all(r["ok"] for r in out["requests"])


def test_split_request_rejects_bad_user_id_before_sending_anything(monkeypatch):
    api = RecordingRequestApi()
    monkeypatch.setattr(server, "get_client", lambda: SimpleNamespace(payment=api))
    with pytest.raises(ToolError, match="numeric Venmo user ID"):
        server.split_request(["1", "oops", "3"], 9.0, "x")
    assert api.calls == []  # nothing sent when any id is invalid


# --- lookup_user ---------------------------------------------------------


def _fake_user(uid="42", username="alice"):
    return SimpleNamespace(
        id=uid,
        username=username,
        display_name="Alice A",
        profile_picture_url="http://pic",
        is_group=False,
    )


def _patch_user_api(monkeypatch, by_username=None, by_id=None):
    user_api = SimpleNamespace(
        get_user_by_username=lambda username: by_username,
        get_user=lambda user_id: by_id,
    )
    monkeypatch.setattr(server, "get_client", lambda: SimpleNamespace(user=user_api))


def test_lookup_user_by_username(monkeypatch):
    _patch_user_api(monkeypatch, by_username=_fake_user())
    out = server.lookup_user(username="alice")
    assert out == {
        "id": "42",
        "username": "alice",
        "display_name": "Alice A",
        "profile_picture_url": "http://pic",
        "is_group": False,
    }


def test_lookup_user_not_found_raises(monkeypatch):
    _patch_user_api(monkeypatch, by_id=None)
    with pytest.raises(ToolError, match="No Venmo user found"):
        server.lookup_user(user_id="999")


# --- request_money -------------------------------------------------------


def test_request_money_posts_charge_and_returns_request_id(monkeypatch):
    api = RecordingApi(
        {"data": {"payment": {"id": "4716790057747324831", "status": "pending"}}}
    )
    monkeypatch.setattr(server, "_venmo_api", api)
    out = server.request_money(
        "42", 285.06, "rent", privacy="friends", assert_friend=False
    )
    assert api.calls == [
        {
            "method": "POST",
            "path": "/payments",
            "body": {
                "user_id": "42",
                "audience": "friends",
                "amount": -285.06,
                "note": "rent",
            },
        }
    ]
    assert out["action"] == "requested"
    assert out["request_id"] == "4716790057747324831"
    assert out["status"] == "pending"
    assert out["amount"] == 285.06


def test_request_money_surfaces_venmo_api_error(monkeypatch):
    def boom(*a, **k):
        raise ToolError("Venmo API error: You cannot request money from this user.")

    monkeypatch.setattr(server, "_venmo_api", boom)
    with pytest.raises(ToolError, match="cannot request money from this user"):
        server.request_money("42", 285.06, "rent", assert_friend=False)


def test_request_money_surfaces_200_status_error_code(monkeypatch):
    api = RecordingApi(
        {
            "data": {
                "error_code": 1396,
                "title": "Request failed",
                "error_msg": "This user can't receive requests right now.",
            }
        }
    )
    monkeypatch.setattr(server, "_venmo_api", api)
    with pytest.raises(ToolError, match="can't receive requests right now"):
        server.request_money("42", 285.06, "rent", assert_friend=False)
