"""Venmo MCP server for Claude Code. Run with: pixi run serve"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

import requests
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from venmo_api import Client, PaymentPrivacy

TOKEN_FILE = Path(__file__).parent / ".venmo-token"

# Raw REST access for endpoints the venmo_api client doesn't expose (incoming
# requests and approving/denying them). Matches the client's host and UA.
VENMO_API_BASE = "https://api.venmo.com/v1"
_USER_AGENT = "Venmo/7.44.0 (iPhone; iOS 13.0; Scale/2.0)"

mcp = MCPServer(
    "venmo",
    title="Venmo",
    version="0.2.0",
    instructions=(
        "Tools for the authenticated user's Venmo account. Look up recipients with "
        "get_friends (preferred) or search_users before send_money/request_money, and "
        "check get_transactions first when the question is whether a payment was already made."
    ),
)

# Annotation presets: read-only lookups vs. money-moving actions.
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
MONEY = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
# Changes account state and can't be undone, but moves no money (e.g. declining a request).
DECLINE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

PRIVACY_MAP = {
    "private": PaymentPrivacy.PRIVATE,
    "friends": PaymentPrivacy.FRIENDS,
    "public": PaymentPrivacy.PUBLIC,
}


class UserBrief(TypedDict):
    id: str
    username: str
    display_name: str


class UserSearchResult(UserBrief):
    profile_picture_url: str | None


class PaymentMethod(TypedDict):
    id: str
    name: str
    role: str | None


class Transaction(TypedDict):
    id: str
    date: str | None
    type: str
    direction: str
    amount: float
    note: str
    status: str
    audience: str
    actor: UserBrief | None
    target: UserBrief | None


class PaymentResult(TypedDict):
    action: str
    amount: float
    user_id: str
    note: str
    message: str


class PendingRequest(TypedDict):
    id: str
    amount: float
    note: str
    date: str | None
    status: str
    from_user: UserBrief | None


class RequestActionResult(TypedDict):
    action: str
    request_id: str
    status: str
    amount: float | None
    message: str


def _epoch_to_iso(value) -> str | None:
    """Venmo returns epoch-second strings. Format as UTC ISO (YYYY-MM-DD HH:MM:SSZ)."""
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%SZ")
    except (ValueError, TypeError, OSError):
        return str(value)


def _user_brief(user) -> UserBrief | None:
    if not user:
        return None
    return {"id": user.id, "username": user.username, "display_name": user.display_name}


def _serialize_txn(t, my_id) -> Transaction:
    """Flatten a Transaction into a JSON-friendly dict, with a direction relative to me."""
    actor_id = t.actor.id if t.actor else None
    # payment_type "pay": actor pays target. "charge": actor requests money from target.
    if t.payment_type == "charge":
        direction = "you_requested" if actor_id == my_id else "requested_from_you"
    else:  # "pay"
        direction = "sent" if actor_id == my_id else "received"
    return {
        "id": t.id,
        "date": _epoch_to_iso(t.date_created),
        "type": t.payment_type,
        "direction": direction,
        "amount": t.amount,
        "note": t.note,
        "status": t.status,
        "audience": t.audience,
        "actor": _user_brief(t.actor),
        "target": _user_brief(t.target),
    }


def get_client() -> Client:
    return Client(access_token=_access_token())


def _access_token() -> str:
    token = os.environ.get("VENMO_ACCESS_TOKEN")
    if not token and TOKEN_FILE.exists():
        token = json.loads(TOKEN_FILE.read_text()).get("access_token")
    if not token:
        raise ToolError(
            "No Venmo access token. Run `pixi run auth` first or set VENMO_ACCESS_TOKEN."
        )
    return token


def _venmo_api(
    method: str, path: str, params: dict | None = None, body: dict | None = None
) -> dict:
    """Call the Venmo REST API directly and return the parsed JSON body."""
    token = _access_token()
    resp = requests.request(
        method,
        f"{VENMO_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": _USER_AGENT},
        params=params,
        json=body,
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError:
        raise ToolError(
            f"Venmo API returned a non-JSON response (HTTP {resp.status_code})."
        ) from None
    if isinstance(data, dict) and data.get("error"):
        raise ToolError(
            f"Venmo API error: {data['error'].get('message', 'unknown error')}"
        )
    return data


def _payment_id(request_id: str) -> str:
    """Venmo payment IDs are numeric strings; reject anything else before touching the API."""
    if not str(request_id).isdigit():
        raise ToolError(
            f"request_id must be a numeric Venmo request ID (from get_pending_requests); got {request_id!r}"
        )
    return str(request_id)


def _target_user_id(user_id: str) -> int:
    """Venmo user IDs are numeric strings; reject anything else before touching the API."""
    try:
        return int(user_id)
    except (TypeError, ValueError):
        raise ToolError(
            f"user_id must be a numeric Venmo user ID (from get_friends or search_users); got {user_id!r}"
        ) from None


def _privacy(value: str) -> PaymentPrivacy:
    try:
        return PRIVACY_MAP[value]
    except KeyError:
        raise ToolError(
            f"privacy must be one of {', '.join(PRIVACY_MAP)}; got {value!r}"
        ) from None


@mcp.tool(title="Search users", annotations=READ_ONLY)
def search_users(query: str, limit: int = 10) -> list[UserSearchResult]:
    """Search for Venmo users by name or username. Use this to find a user's ID before sending/requesting money. Prefer get_friends when the person is likely already a friend."""
    client = get_client()
    users = client.user.search_for_users(query=query, limit=limit)
    return [
        {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "profile_picture_url": user.profile_picture_url,
        }
        for user in users
    ]


@mcp.tool(title="Send money", annotations=MONEY)
def send_money(
    user_id: str,
    amount: float,
    note: str,
    privacy: str = "private",
    funding_source_id: str = "",
) -> PaymentResult:
    """Send money to a Venmo user. Moves real money. Requires user_id (from get_friends or search_users), amount, and a note. Privacy: private, friends, or public. Optionally specify funding_source_id from get_payment_methods."""
    client = get_client()
    kwargs = {
        "amount": amount,
        "note": note,
        "target_user_id": _target_user_id(user_id),
        "privacy_setting": _privacy(privacy),
    }
    if funding_source_id:
        kwargs["funding_source_id"] = funding_source_id
    if not client.payment.send_money(**kwargs):
        raise ToolError("Payment failed.")
    return {
        "action": "sent",
        "amount": amount,
        "user_id": user_id,
        "note": note,
        "message": f"Sent ${amount:.2f} to user {user_id} with note: {note}",
    }


@mcp.tool(title="Request money", annotations=MONEY)
def request_money(
    user_id: str,
    amount: float,
    note: str,
    privacy: str = "private",
) -> PaymentResult:
    """Request money from a Venmo user. Requires user_id (from get_friends or search_users), amount, and a note. Privacy: private, friends, or public."""
    client = get_client()
    if not client.payment.request_money(
        amount=amount,
        note=note,
        target_user_id=_target_user_id(user_id),
        privacy_setting=_privacy(privacy),
    ):
        raise ToolError("Request failed.")
    return {
        "action": "requested",
        "amount": amount,
        "user_id": user_id,
        "note": note,
        "message": f"Requested ${amount:.2f} from user {user_id} with note: {note}",
    }


@mcp.tool(title="Payment methods", annotations=READ_ONLY)
def get_payment_methods() -> list[PaymentMethod]:
    """List available payment methods (Venmo balance, bank accounts, cards)."""
    client = get_client()
    return [
        {"id": m.id, "name": m.name, "role": m.role.value if m.role else None}
        for m in client.payment.get_payment_methods()
    ]


@mcp.tool(title="Friends", annotations=READ_ONLY)
def get_friends(limit: int = 50) -> list[UserBrief]:
    """List the authenticated user's Venmo friends. Useful for finding the right user to send money to."""
    client = get_client()
    profile = client.my_profile()
    friends = client.user.get_user_friends_list(user_id=profile.id, limit=limit)
    return [
        {"id": f.id, "username": f.username, "display_name": f.display_name}
        for f in friends
    ]


@mcp.tool(title="Transactions", annotations=READ_ONLY)
def get_transactions(
    limit: int = 20,
    with_user_id: str = "",
    note_contains: str = "",
    before_id: str = "",
) -> list[Transaction]:
    """List your recent Venmo transactions (payments and requests), newest first.

    Use this to check payment history, e.g. "did I already pay X?" or "find the cleaning payment".
    Each entry includes date (UTC), amount, note, status, payment type, the other party, and a
    `direction` relative to you: "sent", "received", "you_requested", or "requested_from_you".

    - with_user_id: only transactions between you and this user (from search_users/get_friends).
    - note_contains: case-insensitive substring filter on the note (applied after fetch).
    - before_id: pass a transaction id to page to older results.
    """
    client = get_client()
    my_id = client.my_profile().id
    before = before_id or None
    if with_user_id:
        page = client.user.get_transaction_between_two_users(
            user_id_one=my_id, user_id_two=with_user_id, limit=limit, before_id=before
        )
    else:
        page = client.user.get_user_transactions(
            user_id=my_id, limit=limit, before_id=before
        )
    txns = list(page) if page else []
    results = [_serialize_txn(t, my_id) for t in txns]
    if note_contains:
        needle = note_contains.lower()
        results = [r for r in results if r["note"] and needle in r["note"].lower()]
    return results


@mcp.tool(title="Pending requests to me", annotations=READ_ONLY)
def get_pending_requests(limit: int = 20) -> list[PendingRequest]:
    """List money requests others sent you that are still pending (incomplete) and awaiting your response.

    These are charges where you are the one asked to pay. Use complete_request to pay one, or
    deny_request to decline it. This is read-only and does not move money.
    """
    my_id = get_client().my_profile().id
    data = _venmo_api("GET", "/payments", params={"action": "charge", "limit": 200})
    items = data.get("data", []) if isinstance(data, dict) else []
    pending = []
    for p in items:
        if len(pending) >= limit:
            break
        target = (p.get("target") or {}).get("user") or {}
        if p.get("status") != "pending" or target.get("id") != my_id:
            continue
        actor = p.get("actor") or {}
        pending.append(
            {
                "id": p.get("id"),
                "amount": p.get("amount"),
                "note": p.get("note"),
                "date": p.get("date_created"),
                "status": p.get("status"),
                "from_user": (
                    {
                        "id": actor.get("id"),
                        "username": actor.get("username"),
                        "display_name": actor.get("display_name"),
                    }
                    if actor
                    else None
                ),
            }
        )
    return pending


@mcp.tool(title="Complete (pay) a request", annotations=MONEY)
def complete_request(request_id: str) -> RequestActionResult:
    """Complete (pay) a pending money request someone sent you, settling it.

    MOVES REAL MONEY from your default Venmo funding source. Get request_id from
    get_pending_requests. Fails if the request is not pending or was not made to you.
    """
    request_id = _payment_id(request_id)
    data = _venmo_api("PUT", f"/payments/{request_id}", body={"action": "approve"})
    payment = data.get("data", {}) if isinstance(data, dict) else {}
    status = payment.get("status", "unknown")
    amount = payment.get("amount")
    return {
        "action": "completed",
        "request_id": request_id,
        "status": status,
        "amount": amount,
        "message": f"Approved request {request_id}; status is now {status}.",
    }


@mcp.tool(title="Deny a request", annotations=DECLINE)
def deny_request(request_id: str) -> RequestActionResult:
    """Decline a pending money request someone sent you, so you are no longer asked to pay it.

    Does not move money, but cannot be undone. Get request_id from get_pending_requests.
    """
    request_id = _payment_id(request_id)
    data = _venmo_api("PUT", f"/payments/{request_id}", body={"action": "deny"})
    payment = data.get("data", {}) if isinstance(data, dict) else {}
    status = payment.get("status", "unknown")
    return {
        "action": "denied",
        "request_id": request_id,
        "status": status,
        "amount": payment.get("amount"),
        "message": f"Denied request {request_id}; status is now {status}.",
    }


@mcp.tool(title="My profile", annotations=READ_ONLY)
def get_my_profile() -> UserBrief:
    """Get the authenticated user's Venmo profile."""
    profile = get_client().my_profile()
    return {
        "id": profile.id,
        "username": profile.username,
        "display_name": profile.display_name,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
