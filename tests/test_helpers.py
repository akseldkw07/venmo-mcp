"""Unit tests for the pure serialization helpers in server.py (no network, no money)."""

import server
from conftest import make_txn, make_user


class TestEpochToIso:
    def test_known_epoch_formats_as_utc(self):
        # 1780887063 -> 2026-06-08 02:51:03 UTC
        assert server._epoch_to_iso("1780887063") == "2026-06-08 02:51:03Z"

    def test_accepts_int(self):
        assert server._epoch_to_iso(1780887063) == "2026-06-08 02:51:03Z"

    def test_none_and_empty_return_none(self):
        assert server._epoch_to_iso(None) is None
        assert server._epoch_to_iso("") is None

    def test_garbage_falls_back_to_str(self):
        assert server._epoch_to_iso("not-a-number") == "not-a-number"


class TestUserBrief:
    def test_none_user(self):
        assert server._user_brief(None) is None

    def test_brief_fields(self):
        u = make_user("42", "alice", "Alice A")
        assert server._user_brief(u) == {
            "id": "42",
            "username": "alice",
            "display_name": "Alice A",
        }


class TestSerializeTxnDirection:
    def test_pay_as_actor_is_sent(self):
        t = make_txn(
            payment_type="pay", actor=make_user("me"), target=make_user("them")
        )
        assert server._serialize_txn(t, "me")["direction"] == "sent"

    def test_pay_as_target_is_received(self):
        t = make_txn(
            payment_type="pay", actor=make_user("them"), target=make_user("me")
        )
        assert server._serialize_txn(t, "me")["direction"] == "received"

    def test_charge_as_actor_is_you_requested(self):
        t = make_txn(
            payment_type="charge", actor=make_user("me"), target=make_user("them")
        )
        assert server._serialize_txn(t, "me")["direction"] == "you_requested"

    def test_charge_as_target_is_requested_from_you(self):
        t = make_txn(
            payment_type="charge", actor=make_user("them"), target=make_user("me")
        )
        assert server._serialize_txn(t, "me")["direction"] == "requested_from_you"


class TestSerializeTxnShape:
    def test_flattens_all_fields(self):
        t = make_txn(
            txn_id="99",
            amount=365.0,
            note="May 29 cleaning",
            status="settled",
            audience="private",
            actor=make_user("me", "andy", "Andy"),
            target=make_user("maria", "mariaandcrew", "Maria Perez"),
        )
        out = server._serialize_txn(t, "me")
        assert out["id"] == "99"
        assert out["amount"] == 365.0
        assert out["note"] == "May 29 cleaning"
        assert out["status"] == "settled"
        assert out["audience"] == "private"
        assert out["actor"]["username"] == "andy"
        assert out["target"]["username"] == "mariaandcrew"
        assert out["date"] == "2026-06-08 02:51:03Z"
