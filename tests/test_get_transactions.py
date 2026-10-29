"""Tests for the get_transactions tool: routing and filtering, with a faked client."""

import server
from conftest import make_txn, make_user


def _txns():
    return [
        make_txn(
            txn_id="1",
            note="May 29 cleaning",
            actor=make_user("me", "andy", "Andy"),
            target=make_user("maria", "mariaandcrew", "Maria Perez"),
        ),
        make_txn(
            txn_id="2",
            note="Outlet, light",
            actor=make_user("me", "andy", "Andy"),
            target=make_user("jose", "jose", "Jose"),
        ),
    ]


def test_default_uses_user_transactions(fake_client_factory):
    client = fake_client_factory(_txns(), my_id="me")
    out = server.get_transactions()
    assert isinstance(out, list) and len(out) == 2
    methods = [c["method"] for c in client.user.calls]
    assert methods == ["get_user_transactions"]


def test_with_user_id_routes_to_between_two_users(fake_client_factory):
    client = fake_client_factory(_txns(), my_id="me")
    server.get_transactions(with_user_id="maria")
    call = client.user.calls[0]
    assert call["method"] == "get_transaction_between_two_users"
    assert call["user_id_one"] == "me"
    assert call["user_id_two"] == "maria"


def test_note_contains_filters_case_insensitively(fake_client_factory):
    fake_client_factory(_txns(), my_id="me")
    out = server.get_transactions(note_contains="CLEANING")
    assert len(out) == 1
    assert out[0]["note"] == "May 29 cleaning"


def test_note_contains_no_match_returns_empty(fake_client_factory):
    fake_client_factory(_txns(), my_id="me")
    out = server.get_transactions(note_contains="zzz")
    assert out == []


def test_limit_and_before_id_are_passed_through(fake_client_factory):
    client = fake_client_factory(_txns(), my_id="me")
    server.get_transactions(limit=5, before_id="abc")
    call = client.user.calls[0]
    assert call["limit"] == 5
    assert call["before_id"] == "abc"


def test_empty_before_id_becomes_none(fake_client_factory):
    client = fake_client_factory(_txns(), my_id="me")
    server.get_transactions()  # before_id defaults to ""
    assert client.user.calls[0]["before_id"] is None


def test_direction_reflects_authenticated_user(fake_client_factory):
    fake_client_factory(_txns(), my_id="me")
    out = server.get_transactions()
    assert all(t["direction"] == "sent" for t in out)
