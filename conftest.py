"""Shared fixtures: fake Venmo client/transaction objects so tests never touch the network."""

from types import SimpleNamespace

import pytest


def make_user(user_id, username="user", display_name="User"):
    return SimpleNamespace(id=user_id, username=username, display_name=display_name)


def make_txn(
    *,
    txn_id="1",
    payment_type="pay",
    amount=10.0,
    note="note",
    status="settled",
    audience="private",
    date_created="1780887063",
    actor=None,
    target=None,
):
    return SimpleNamespace(
        id=txn_id,
        payment_type=payment_type,
        amount=amount,
        note=note,
        status=status,
        audience=audience,
        date_created=date_created,
        actor=actor,
        target=target,
    )


class FakeUserApi:
    """Records which read method was called and returns a canned page (a plain list)."""

    def __init__(self, txns):
        self._txns = txns
        self.calls = []

    def get_user_transactions(self, user_id=None, limit=50, before_id=None):
        self.calls.append(
            {
                "method": "get_user_transactions",
                "user_id": user_id,
                "limit": limit,
                "before_id": before_id,
            }
        )
        return list(self._txns)

    def get_transaction_between_two_users(
        self, user_id_one=None, user_id_two=None, limit=50, before_id=None
    ):
        self.calls.append(
            {
                "method": "get_transaction_between_two_users",
                "user_id_one": user_id_one,
                "user_id_two": user_id_two,
                "limit": limit,
                "before_id": before_id,
            }
        )
        return list(self._txns)


class FakeClient:
    def __init__(self, txns, my_id):
        self.user = FakeUserApi(txns)
        self._my_id = my_id

    def my_profile(self):
        return SimpleNamespace(id=self._my_id)


@pytest.fixture
def fake_client_factory(monkeypatch):
    """Patch server.get_client to return a FakeClient; hand the client back for assertions."""
    import server

    def _factory(txns, my_id="me"):
        client = FakeClient(txns, my_id)
        monkeypatch.setattr(server, "get_client", lambda: client)
        return client

    return _factory


@pytest.fixture(autouse=True)
def _no_real_venmo_token(monkeypatch, tmp_path):
    """Hide the real token so an unpatched tool can never call the live Venmo API."""
    import server

    monkeypatch.delenv("VENMO_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("VENMO_FUNDING_SOURCE_ID", raising=False)
    monkeypatch.setattr(server, "TOKEN_FILE", tmp_path / "no-token")


class RecordingApi:
    """Stands in for server._venmo_api: records each call and returns a canned response.

    `responses` is either a dict keyed by (method, path), or one response for every
    call. A callable response is called with the request params. Each recorded call
    has method, path and body, plus params when the request had any.
    """

    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def __call__(self, method, path, params=None, body=None):
        call = {"method": method, "path": path, "body": body}
        if params is not None:
            call["params"] = params
        self.calls.append(call)
        keyed = isinstance(self._responses, dict) and all(
            isinstance(k, tuple) for k in self._responses
        )
        response = self._responses[(method, path)] if keyed else self._responses
        return response(params) if callable(response) else response


@pytest.fixture
def no_recent_duplicates(monkeypatch):
    """For tests that aren't about the duplicate guard: report no recent matches."""
    import server

    monkeypatch.setattr(server, "_recent_requests", lambda *a: [])
    monkeypatch.setattr(server, "_recent_sends", lambda *a: [])
