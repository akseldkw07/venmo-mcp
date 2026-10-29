"""Venmo MCP server for Claude Code. Run with: pixi run serve"""

import functools
import json
import os
from datetime import datetime, timedelta, timezone
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


class SendResult(PaymentResult):
    payment_id: str | None
    status: str


class RequestResult(PaymentResult):
    request_id: str | None
    status: str


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


class OutgoingRequest(TypedDict):
    id: str
    amount: float
    note: str
    date: str | None
    status: str
    to_user: UserBrief | None


class SplitItem(TypedDict):
    user_id: str
    amount: float
    ok: bool
    error: str | None


class SplitResult(TypedDict):
    total: float
    note: str
    count: int
    requests: list[SplitItem]


class UserProfile(TypedDict):
    id: str
    username: str
    display_name: str
    profile_picture_url: str | None
    is_group: bool


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


def _surface_errors(fn):
    """Re-raise venmo_api and network failures as ToolErrors so the caller sees why.

    MCP reports any other exception as a bare "Error executing tool <name>", which
    hides whether Venmo rejected the call or it never arrived. Genuine bugs still crash.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except requests.RequestException as e:
            raise ToolError(
                f"Could not reach Venmo ({type(e).__name__}): {e}. "
                "The call may or may not have reached Venmo."
            ) from e
        except Exception as e:
            if type(e).__module__.startswith("venmo_api"):
                raise ToolError(f"Venmo error ({type(e).__name__}): {e}") from e
            raise

    return wrapper


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


_FRIENDS_PAGE_SIZE = 500


def _friend_ids() -> set[str]:
    """All of the authenticated user's friend ids, paging until Venmo returns an empty page."""
    my_id = get_client().my_profile().id
    ids: set[str] = set()
    offset = 0
    while True:
        data = _venmo_api(
            "GET",
            f"/users/{my_id}/friends",
            params={"limit": _FRIENDS_PAGE_SIZE, "offset": offset},
        )
        page = data.get("data", []) if isinstance(data, dict) else []
        if not page:
            return ids
        ids.update(str(f["id"]) for f in page if f and f.get("id"))
        offset += len(page)


def _assert_friends(user_ids: list[str]) -> None:
    friends = _friend_ids()
    strangers = [u for u in user_ids if str(u) not in friends]
    if strangers:
        raise ToolError(
            f"Refusing: user {', '.join(strangers)} is not one of your Venmo friends. "
            "Confirm the recipient with lookup_user; only retry with assert_friend=False "
            "if the user explicitly approves paying/requesting a non-friend."
        )


DUPLICATE_WINDOW = timedelta(minutes=10)
_INACTIVE_REQUEST_STATUSES = {"cancelled", "denied", "expired"}


def _parse_venmo_time(value) -> datetime | None:
    """Venmo dates come as epoch-second strings or naive UTC ISO strings."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        pass
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_recent(value) -> bool:
    when = _parse_venmo_time(value)
    return when is not None and datetime.now(timezone.utc) - when <= DUPLICATE_WINDOW


def _recent_requests(target: str, amount: float, note: str) -> list[str]:
    """Ids of my still-active requests to target with this amount and note, made recently."""
    my_id = str(get_client().my_profile().id)
    data = _venmo_api("GET", "/payments", params={"action": "charge", "limit": 50})
    items = data.get("data", []) if isinstance(data, dict) else []
    return [
        str(p["id"])
        for p in items
        if str((p.get("actor") or {}).get("id")) == my_id
        and str(((p.get("target") or {}).get("user") or {}).get("id")) == target
        and round(abs(p.get("amount") or 0), 2) == round(abs(amount), 2)
        and p.get("note") == note
        and p.get("status") not in _INACTIVE_REQUEST_STATUSES
        and _is_recent(p.get("date_created"))
    ]


def _recent_sends(target: str, amount: float, note: str) -> list[str]:
    """Transaction ids of my recent payments to target with this amount and note."""
    client = get_client()
    my_id = str(client.my_profile().id)
    feed = client.user.get_transaction_between_two_users(
        user_id_one=my_id, user_id_two=target, limit=20
    )
    return [
        str(t.id)
        for t in feed or []
        if t.payment_type == "pay"
        and t.actor is not None
        and str(t.actor.id) == my_id
        and round(abs(t.amount or 0), 2) == round(abs(amount), 2)
        and t.note == note
        and _is_recent(t.date_created)
    ]


def _creation_status(find, before: list[str], kind: str, lookup_tool: str) -> str:
    """After a failed write, say whether Venmo created it anyway (a match not seen before)."""
    try:
        new = [i for i in find() if i not in before]
    except ToolError:
        return (
            f"created: unknown (could not check); check {lookup_tool} before retrying."
        )
    if new:
        return f"created: true ({kind} {new[0]}) despite the error; do not retry."
    return f"created: false (no new matching {kind} found); safe to retry."


def _post_payment(body: dict) -> dict:
    """POST /payments and return the created payment, raising ToolError on any failure."""
    data = _venmo_api("POST", "/payments", body=body)
    payload = data.get("data", {}) if isinstance(data, dict) else {}
    # Venmo reports some payment failures as HTTP 200 with an error_code.
    if payload.get("error_code"):
        detail = payload.get("error_msg") or payload.get("title") or "unknown error"
        raise ToolError(f"Venmo API error ({payload['error_code']}): {detail}")
    return payload.get("payment") or {}


def _default_funding_source_id() -> str:
    """VENMO_FUNDING_SOURCE_ID if set, else the account's Venmo-default payment method."""
    configured = os.environ.get("VENMO_FUNDING_SOURCE_ID")
    if configured:
        return configured
    data = _venmo_api("GET", "/payment-methods")
    methods = data.get("data", []) if isinstance(data, dict) else []
    for m in methods:
        if m and m.get("peer_payment_role") == "default" and m.get("id"):
            return str(m["id"])
    raise ToolError(
        "No default Venmo funding source found; pass funding_source_id from get_payment_methods."
    )


@mcp.tool(title="Search users", annotations=READ_ONLY)
@_surface_errors
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
@_surface_errors
def send_money(
    user_id: str,
    amount: float,
    note: str,
    privacy: str = "private",
    funding_source_id: str = "",
    assert_friend: bool = True,
    force: bool = False,
) -> SendResult:
    """Send money to a Venmo user. Moves real money. Requires user_id (from get_friends or search_users), amount, and a note. Privacy: private, friends, or public. Optionally specify funding_source_id from get_payment_methods (defaults to VENMO_FUNDING_SOURCE_ID if configured, else your Venmo default funding source). Returns the new payment's id. assert_friend (default true) refuses unless the recipient is one of your Venmo friends; only pass false when the user has explicitly confirmed the recipient. Refuses an identical payment (same recipient, amount and note) made in the last 10 minutes unless force=true. On failure the error says whether the payment was created anyway (created: true/false/unknown); never retry after created: true."""
    target = str(_target_user_id(user_id))
    audience = _privacy(privacy).value
    if assert_friend:
        _assert_friends([target])
    existing = _recent_sends(target, amount, note)
    if existing and not force:
        raise ToolError(
            f"Refusing: an identical payment {existing[0]} (same recipient, amount and "
            f"note) was made in the last {DUPLICATE_WINDOW.seconds // 60} minutes. "
            "Pass force=True only if the user confirms sending it again."
        )
    body = {
        "user_id": target,
        "audience": audience,
        "amount": abs(amount),
        "note": note,
        "funding_source_id": funding_source_id or _default_funding_source_id(),
    }
    try:
        payment = _post_payment(body)
    except ToolError as e:
        status = _creation_status(
            lambda: _recent_sends(target, amount, note),
            existing,
            "payment",
            "get_transactions",
        )
        raise ToolError(f"{e} {status}") from e
    payment_id = str(payment["id"]) if payment.get("id") else None
    return {
        "action": "sent",
        "amount": amount,
        "user_id": user_id,
        "note": note,
        "payment_id": payment_id,
        "status": payment.get("status", "unknown"),
        "message": f"Sent ${amount:.2f} to user {user_id} with note: {note}"
        + (f" (payment id {payment_id})" if payment_id else ""),
    }


@mcp.tool(title="Split a bill", annotations=MONEY)
@_surface_errors
def split_request(
    user_ids: list[str],
    total: float,
    note: str,
    privacy: str = "private",
    assert_friend: bool = True,
) -> SplitResult:
    """Request an even split of a total from several users at once (one request each).

    Splits `total` evenly to the cent across user_ids (any leftover cents go to the
    earliest users), then sends each person a money request with `note`. All user_ids
    are validated before anything is sent; if an individual request fails, the others
    still go through and the failure is reported per user. Privacy: private, friends, public.
    assert_friend (default true) refuses the whole split, sending nothing, unless every
    user is one of your Venmo friends; only pass false when the user has explicitly
    confirmed the recipients.
    """
    if not user_ids:
        raise ToolError("user_ids must contain at least one user.")
    targets = [_target_user_id(u) for u in user_ids]  # validates all up front
    privacy_setting = _privacy(privacy)
    if assert_friend:
        _assert_friends([str(t) for t in targets])

    cents = round(total * 100)
    base, remainder = divmod(cents, len(user_ids))
    amounts = [(base + (1 if i < remainder else 0)) / 100 for i in range(len(user_ids))]

    client = get_client()
    requests_out: list[SplitItem] = []
    for user_id, target, amount in zip(user_ids, targets, amounts):
        try:
            ok = client.payment.request_money(
                amount=amount,
                note=note,
                target_user_id=target,
                privacy_setting=privacy_setting,
            )
            requests_out.append(
                {"user_id": user_id, "amount": amount, "ok": bool(ok), "error": None}
            )
        except Exception as e:
            requests_out.append(
                {"user_id": user_id, "amount": amount, "ok": False, "error": str(e)}
            )
    return {
        "total": total,
        "note": note,
        "count": len(user_ids),
        "requests": requests_out,
    }


@mcp.tool(title="Request money", annotations=MONEY)
@_surface_errors
def request_money(
    user_id: str,
    amount: float,
    note: str,
    privacy: str = "private",
    assert_friend: bool = True,
    force: bool = False,
) -> RequestResult:
    """Request money from a Venmo user. Requires user_id (from get_friends or search_users), amount, and a note. Privacy: private, friends, or public. Returns the new request's id (usable with remind_request / cancel_request). assert_friend (default true) refuses unless the recipient is one of your Venmo friends; only pass false when the user has explicitly confirmed the recipient. Refuses an identical active request (same person, amount and note) made in the last 10 minutes unless force=true. On failure the error says whether the request was created anyway (created: true/false/unknown); never retry after created: true."""
    target = str(_target_user_id(user_id))
    audience = _privacy(privacy).value
    if assert_friend:
        _assert_friends([target])
    existing = _recent_requests(target, amount, note)
    if existing and not force:
        raise ToolError(
            f"Refusing: an identical request {existing[0]} (same person, amount and "
            f"note) was made in the last {DUPLICATE_WINDOW.seconds // 60} minutes. "
            "Pass force=True only if the user confirms requesting again."
        )
    body = {
        "user_id": target,
        "audience": audience,
        "amount": -abs(amount),  # negative amount = charge (request)
        "note": note,
    }
    try:
        payment = _post_payment(body)
    except ToolError as e:
        status = _creation_status(
            lambda: _recent_requests(target, amount, note),
            existing,
            "request",
            "get_outgoing_requests",
        )
        raise ToolError(f"{e} {status}") from e
    request_id = str(payment["id"]) if payment.get("id") else None
    status = payment.get("status", "unknown")
    return {
        "action": "requested",
        "amount": amount,
        "user_id": user_id,
        "note": note,
        "request_id": request_id,
        "status": status,
        "message": f"Requested ${amount:.2f} from user {user_id} with note: {note}"
        + (f" (request id {request_id})" if request_id else ""),
    }


@mcp.tool(title="Payment methods", annotations=READ_ONLY)
@_surface_errors
def get_payment_methods() -> list[PaymentMethod]:
    """List available payment methods (Venmo balance, bank accounts, cards)."""
    client = get_client()
    return [
        {"id": m.id, "name": m.name, "role": m.role.value if m.role else None}
        for m in client.payment.get_payment_methods()
    ]


@mcp.tool(title="Friends", annotations=READ_ONLY)
@_surface_errors
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
@_surface_errors
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
@_surface_errors
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


@mcp.tool(title="Outgoing requests", annotations=READ_ONLY)
@_surface_errors
def get_outgoing_requests(limit: int = 20) -> list[OutgoingRequest]:
    """List money requests YOU sent that are still pending (unpaid) — who owes you and how much.

    The mirror of get_pending_requests. Use remind_request to nudge someone or
    cancel_request to withdraw one. Read-only; does not move money.
    """
    charges = get_client().payment.get_charge_payments(limit=200) or []
    out: list[OutgoingRequest] = []
    for p in charges:
        if len(out) >= limit:
            break
        status = getattr(p.status, "value", p.status)
        if status != "pending":
            continue
        out.append(
            {
                "id": p.id,
                "amount": p.amount,
                "note": p.note,
                "date": _epoch_to_iso(p.date_created),
                "status": status,
                "to_user": _user_brief(p.target),
            }
        )
    return out


@mcp.tool(title="Cancel a request", annotations=DECLINE)
@_surface_errors
def cancel_request(request_id: str) -> RequestActionResult:
    """Cancel (withdraw) a pending request YOU made, so the other person is no longer asked to pay.

    Cannot be undone. Get request_id from get_outgoing_requests.
    """
    request_id = _payment_id(request_id)
    try:
        get_client().payment.cancel_payment(payment_id=request_id)
    except Exception as e:
        raise ToolError(f"Could not cancel request {request_id}: {e}") from e
    return {
        "action": "cancelled",
        "request_id": request_id,
        "status": "cancelled",
        "amount": None,
        "message": f"Cancelled request {request_id}.",
    }


@mcp.tool(title="Remind about a request", annotations=MONEY)
@_surface_errors
def remind_request(request_id: str) -> RequestActionResult:
    """Send a reminder for a pending request YOU made, nudging the other person to pay.

    Doesn't move money, but pings someone. Get request_id from get_outgoing_requests.
    """
    request_id = _payment_id(request_id)
    try:
        get_client().payment.remind_payment(payment_id=request_id)
    except Exception as e:
        raise ToolError(f"Could not remind on request {request_id}: {e}") from e
    return {
        "action": "reminded",
        "request_id": request_id,
        "status": "reminded",
        "amount": None,
        "message": f"Sent a reminder for request {request_id}.",
    }


@mcp.tool(title="Complete (pay) a request", annotations=MONEY)
@_surface_errors
def complete_request(
    request_id: str, assert_friend: bool = True
) -> RequestActionResult:
    """Complete (pay) a pending money request someone sent you, settling it.

    MOVES REAL MONEY from your default Venmo funding source. Get request_id from
    get_pending_requests. Fails if the request is not pending or was not made to you.
    assert_friend (default true) refuses unless the requester is one of your Venmo
    friends; only pass false when the user has explicitly confirmed the requester.
    """
    request_id = _payment_id(request_id)
    if assert_friend:
        existing = _venmo_api("GET", f"/payments/{request_id}")
        payment = existing.get("data", {}) if isinstance(existing, dict) else {}
        requester = (payment.get("actor") or {}).get("id")
        if not requester:
            raise ToolError(f"Could not determine who sent request {request_id}.")
        _assert_friends([str(requester)])
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
@_surface_errors
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


@mcp.tool(title="Look up a user", annotations=READ_ONLY)
@_surface_errors
def lookup_user(user_id: str = "", username: str = "") -> UserProfile:
    """Look up a Venmo user's public profile by user_id or username (confirm who you're paying).

    Provide exactly one of user_id or username. Read-only.
    """
    client = get_client()
    if username:
        user = client.user.get_user_by_username(username)
    elif user_id:
        user = client.user.get_user(user_id)
    else:
        raise ToolError("Provide a user_id or a username to look up.")
    if not user:
        raise ToolError(f"No Venmo user found for {username or user_id!r}.")
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "profile_picture_url": user.profile_picture_url,
        "is_group": bool(user.is_group),
    }


@mcp.tool(title="My profile", annotations=READ_ONLY)
@_surface_errors
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
