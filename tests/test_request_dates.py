"""requested_at / completed_at on transactions and request listings.

How Venmo stores a paid request (seen live 2026-11-23): the transaction feed shows a *new*
payment, with its own id, created at the moment of payment. The original request lives only in
GET /payments?action=charge, with the same people and amount and a date_completed equal to the
feed entry's time (within a second or two). There is no field linking the two. Venmo's raw
timestamps are UTC strings; venmo_api's parsed epochs read them as local time.
"""

from types import SimpleNamespace

import pytest

import server
from conftest import RecordingApi, make_txn, make_user

ME = make_user("me", "me", "Me")
OTHER = make_user("7", "other", "Other")


def _feed_charge(
    payment_id, paid, *, amount=10.0, actor=OTHER, target=ME, status="settled"
):
    """A feed entry for a request: its payment was created when it was paid."""
    return make_txn(
        payment_type="charge",
        status=status,
        amount=amount,
        actor=actor,
        target=target,
        payment_id=payment_id,
        json={
            "id": f"s{payment_id}",
            "date_created": paid,
            "payment": {"id": payment_id, "date_created": paid, "date_completed": paid},
        },
    )


def _original(
    req_id, created, completed, *, amount=10.0, actor="7", target="me", status="settled"
):
    """The original request as GET /payments?action=charge returns it."""
    return {
        "id": req_id,
        "status": status,
        "amount": amount,
        "date_created": created,
        "date_completed": completed,
        "actor": {"id": actor},
        "target": {"user": {"id": target}},
    }


def _charges_api(*pages):
    """RecordingApi serving /payments pages in order (one list per page)."""
    remaining = list(pages)
    return RecordingApi(lambda params: {"data": remaining.pop(0) if remaining else []})


def test_paid_request_gets_original_request_time(fake_client_factory, monkeypatch):
    fake_client_factory([_feed_charge("900", "2026-10-29T01:04:01")])
    api = _charges_api([_original("100", "2026-10-26T22:19:51", "2026-10-29T01:04:01")])
    monkeypatch.setattr(server, "_venmo_api", api)
    out = server.get_transactions()[0]
    assert out["requested_at"] == "2026-10-26 22:19:51Z"
    assert out["completed_at"] == "2026-10-29 01:04:01Z"
    assert len(api.calls) == 1
    assert api.calls[0]["path"] == "/payments"
    assert api.calls[0]["params"] == {"action": "charge", "limit": 200}


def test_completion_a_couple_seconds_apart_still_matches(
    fake_client_factory, monkeypatch
):
    fake_client_factory([_feed_charge("900", "2026-11-23T08:49:27")])
    monkeypatch.setattr(
        server,
        "_venmo_api",
        _charges_api([_original("100", "2026-11-22T10:33:02", "2026-11-23T08:49:29")]),
    )
    assert server.get_transactions()[0]["requested_at"] == "2026-11-22 10:33:02Z"


def test_same_amount_paid_seconds_apart_each_get_their_own(
    fake_client_factory, monkeypatch
):
    fake_client_factory(
        [
            _feed_charge("901", "2026-10-19T21:02:09"),
            _feed_charge("902", "2026-10-19T21:02:04"),
        ]
    )
    monkeypatch.setattr(
        server,
        "_venmo_api",
        _charges_api(
            [
                _original("A", "2026-10-19T17:29:40", "2026-10-19T21:02:04"),
                _original("B", "2026-10-19T17:29:07", "2026-10-19T21:02:09"),
            ]
        ),
    )
    out = server.get_transactions()
    assert [r["requested_at"] for r in out] == [
        "2026-10-19 17:29:07Z",
        "2026-10-19 17:29:40Z",
    ]


@pytest.mark.parametrize(
    "original",
    [
        _original("100", "2026-10-25T15:34:16", "2026-10-29T01:04:01", amount=11.0),
        _original("100", "2026-10-25T15:34:16", "2026-10-29T01:04:01", actor="someone"),
        _original(
            "100", "2026-10-25T15:34:16", "2026-10-29T01:04:01", actor="me", target="7"
        ),
        _original("100", "2026-10-25T15:34:16", "2026-10-29T01:05:01"),
    ],
    ids=["other-amount", "other-person", "other-direction", "a-minute-apart"],
)
def test_no_match_leaves_request_time_null(fake_client_factory, monkeypatch, original):
    fake_client_factory([_feed_charge("900", "2026-10-29T01:04:01")])
    monkeypatch.setattr(server, "_venmo_api", _charges_api([original]))
    out = server.get_transactions()[0]
    assert out["requested_at"] is None
    assert out["completed_at"] == "2026-10-29 01:04:01Z"


def test_equally_close_candidates_are_ambiguous(fake_client_factory, monkeypatch):
    fake_client_factory([_feed_charge("900", "2026-10-29T01:04:01")])
    monkeypatch.setattr(
        server,
        "_venmo_api",
        _charges_api(
            [
                _original("A", "2026-10-20T00:00:00", "2026-10-29T01:04:01"),
                _original("B", "2026-10-21T00:00:00", "2026-10-29T01:04:01"),
            ]
        ),
    )
    assert server.get_transactions()[0]["requested_at"] is None


def test_feed_entry_that_is_the_request_itself_matches_by_id(
    fake_client_factory, monkeypatch
):
    txn = make_txn(
        payment_type="charge",
        status="pending",
        actor=ME,
        target=OTHER,
        payment_id="100",
        json={
            "id": "s",
            "payment": {
                "id": "100",
                "date_created": "2026-11-07T09:15:00.123000",
                "date_completed": None,
            },
        },
    )
    fake_client_factory([txn])
    monkeypatch.setattr(
        server,
        "_venmo_api",
        _charges_api(
            [
                _original(
                    "100",
                    "2026-11-07T09:15:00.123000",
                    None,
                    actor="me",
                    target="7",
                    status="pending",
                )
            ]
        ),
    )
    out = server.get_transactions()[0]
    assert out["requested_at"] == "2026-11-07 09:15:00Z"
    assert out["completed_at"] is None


def test_pages_back_until_every_request_is_matched(fake_client_factory, monkeypatch):
    fake_client_factory([_feed_charge("900", "2026-10-29T01:04:01")])
    page1 = [_original("X", "2026-10-28T00:00:00", "2026-10-28T01:00:00", amount=3.0)]
    page2 = [_original("100", "2026-08-01T12:00:00", "2026-10-29T01:04:01")]
    api = _charges_api(page1, page2, [])
    monkeypatch.setattr(server, "_venmo_api", api)
    assert server.get_transactions()[0]["requested_at"] == "2026-08-01 12:00:00Z"
    assert len(api.calls) == 2
    assert api.calls[1]["params"] == {
        "action": "charge",
        "limit": 200,
        "before": "2026-10-28T00:00:00",
    }


def test_paging_is_capped(fake_client_factory, monkeypatch):
    fake_client_factory([_feed_charge("900", "2026-10-29T01:04:01")])
    filler = [_original("X", "2026-01-01T00:00:00", "2026-01-01T00:00:01", amount=3.0)]
    api = _charges_api(*[filler] * 10)
    monkeypatch.setattr(server, "_venmo_api", api)
    assert server.get_transactions()[0]["requested_at"] is None
    assert len(api.calls) == server.CHARGE_LOOKUP_PAGES


def test_no_lookup_without_requests(fake_client_factory, monkeypatch):
    txn = make_txn(
        payment_type="pay",
        actor=ME,
        target=OTHER,
        payment_id="103",
        json={
            "id": "s",
            "payment": {
                "id": "103",
                "date_created": "2026-11-09T12:00:00",
                "date_completed": "2026-11-09T12:00:01",
            },
        },
    )
    fake_client_factory([txn])
    api = RecordingApi({"data": []})
    monkeypatch.setattr(server, "_venmo_api", api)
    out = server.get_transactions()[0]
    assert out["requested_at"] == out["completed_at"] == "2026-11-09 12:00:01Z"
    assert api.calls == []


def test_date_field_is_unchanged(fake_client_factory, monkeypatch):
    fake_client_factory([_feed_charge("900", "2026-10-29T01:04:01")])
    monkeypatch.setattr(server, "_venmo_api", _charges_api([]))
    assert server.get_transactions()[0]["date"] == server._epoch_to_iso("1780887063")


def test_pending_requests_have_both_dates(monkeypatch):
    monkeypatch.setattr(
        server,
        "get_client",
        lambda: SimpleNamespace(my_profile=lambda: SimpleNamespace(id="me")),
    )
    charge = _original("1", "2026-10-26T22:19:51", None, actor="99", status="pending")
    charge.update(note="n", actor={"id": "99", "username": "a", "display_name": "A"})
    monkeypatch.setattr(server, "_venmo_api", lambda *a, **k: {"data": [charge]})
    out = server.get_pending_requests()[0]
    assert out["requested_at"] == "2026-10-26 22:19:51Z"
    assert out["completed_at"] is None


def test_outgoing_requests_have_both_dates(monkeypatch):
    payment = SimpleNamespace(
        id="1",
        status="pending",
        amount=1.0,
        note="n",
        date_created=1780887063,
        actor=ME,
        target=OTHER,
        _json={
            "id": "1",
            "date_created": "2026-11-20T08:00:00",
            "date_completed": None,
        },
    )
    client = SimpleNamespace(
        payment=SimpleNamespace(get_charge_payments=lambda limit=100: [payment]),
        my_profile=lambda: SimpleNamespace(id="me"),
    )
    monkeypatch.setattr(server, "get_client", lambda: client)
    out = server.get_outgoing_requests()[0]
    assert out["requested_at"] == "2026-11-20 08:00:00Z"
    assert out["completed_at"] is None
