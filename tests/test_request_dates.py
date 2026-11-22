"""requested_at / completed_at on transactions and request listings.

Venmo's raw timestamps are UTC strings ("2026-10-25T14:00:00"). These come from the raw JSON,
not venmo_api's parsed epochs, which read the strings as local time.
"""

from types import SimpleNamespace

import server
from conftest import RecordingApi, make_txn, make_user

ME = make_user("me", "me", "Me")
OTHER = make_user("7", "other", "Other")


def _story(payment_id, *, created, completed, story_created="2026-10-29T18:30:00"):
    return {
        "id": f"s{payment_id}",
        "date_created": story_created,
        "payment": {
            "id": payment_id,
            "date_created": created,
            "date_completed": completed,
        },
    }


def test_paid_request_has_request_and_completion_times(fake_client_factory):
    txn = make_txn(
        payment_type="charge",
        actor=ME,
        target=OTHER,
        payment_id="101",
        json=_story(
            "101", created="2026-10-25T14:00:00", completed="2026-10-29T18:30:00"
        ),
    )
    fake_client_factory([txn])
    out = server.get_transactions()[0]
    assert out["requested_at"] == "2026-10-25 14:00:00Z"
    assert out["completed_at"] == "2026-10-29 18:30:00Z"


def test_date_field_is_unchanged(fake_client_factory):
    txn = make_txn(
        payment_type="charge",
        actor=ME,
        target=OTHER,
        json=_story(
            "101", created="2026-10-25T14:00:00", completed="2026-10-29T18:30:00"
        ),
    )
    fake_client_factory([txn])
    assert server.get_transactions()[0]["date"] == server._epoch_to_iso("1780887063")


def test_pending_request_has_no_completion_time(fake_client_factory):
    txn = make_txn(
        payment_type="charge",
        status="pending",
        actor=ME,
        target=OTHER,
        payment_id="102",
        json=_story("102", created="2026-11-07T09:15:00.123000", completed=None),
    )
    fake_client_factory([txn])
    out = server.get_transactions()[0]
    assert out["requested_at"] == "2026-11-07 09:15:00Z"
    assert out["completed_at"] is None


def test_direct_payment_both_dates_equal_payment_time(fake_client_factory):
    txn = make_txn(
        payment_type="pay",
        actor=ME,
        target=OTHER,
        payment_id="103",
        json=_story(
            "103", created="2026-11-09T12:00:00", completed="2026-11-09T12:00:01"
        ),
    )
    fake_client_factory([txn])
    out = server.get_transactions()[0]
    assert out["requested_at"] == out["completed_at"] == "2026-11-09 12:00:01Z"


def test_missing_request_time_is_looked_up_in_one_batched_call(
    fake_client_factory, monkeypatch
):
    no_dates = {"id": "s", "payment": {"id": None}}
    txns = [
        make_txn(
            payment_type="charge",
            actor=ME,
            target=OTHER,
            payment_id=pid,
            json={**no_dates, "payment": {"id": pid}},
        )
        for pid in ("201", "202")
    ]
    fake_client_factory(txns)
    api = RecordingApi(
        {
            "data": [
                {
                    "id": "201",
                    "date_created": "2026-10-25T14:00:00",
                    "date_completed": "2026-10-29T18:30:00",
                },
                {
                    "id": "202",
                    "date_created": "2026-10-29T10:00:00",
                    "date_completed": None,
                },
            ]
        }
    )
    monkeypatch.setattr(server, "_venmo_api", api)
    out = server.get_transactions()
    assert len(api.calls) == 1
    assert api.calls[0]["path"] == "/payments"
    assert api.calls[0]["params"]["action"] == "charge"
    assert [r["requested_at"] for r in out] == [
        "2026-10-25 14:00:00Z",
        "2026-10-29 10:00:00Z",
    ]
    assert out[0]["completed_at"] == "2026-10-29 18:30:00Z"


def test_no_lookup_when_feed_has_the_dates(fake_client_factory, monkeypatch):
    txn = make_txn(
        payment_type="charge",
        actor=ME,
        target=OTHER,
        payment_id="1",
        json=_story("1", created="2026-10-25T14:00:00", completed=None),
    )
    fake_client_factory([txn])
    api = RecordingApi({"data": []})
    monkeypatch.setattr(server, "_venmo_api", api)
    server.get_transactions()
    assert api.calls == []


def test_pending_requests_have_both_dates(monkeypatch):
    monkeypatch.setattr(
        server,
        "get_client",
        lambda: SimpleNamespace(my_profile=lambda: SimpleNamespace(id="me")),
    )
    charge = {
        "id": "1",
        "status": "pending",
        "amount": 1.0,
        "note": "n",
        "date_created": "2026-10-26T22:19:51",
        "date_completed": None,
        "actor": {"id": "99", "username": "a", "display_name": "A"},
        "target": {"user": {"id": "me"}},
    }
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
