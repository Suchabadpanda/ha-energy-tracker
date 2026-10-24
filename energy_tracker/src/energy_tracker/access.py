"""Who may change things.

Home Assistant's ingress tells the app who opened it (X-Remote-User-Id, -Name and
-Display-Name headers). Viewing is open to anyone signed into Home Assistant; saving or
deleting anything (rates, bills, notes, settings, restores, imports) needs one of:

- the add-on option `edit_access`: "everyone", "admins" (the default: Home Assistant
  administrators and the owner) or "owner" (only the owner);
- or a name in `edit_users` (a Home Assistant username, display name or user id), which
  is always allowed whatever `edit_access` says.

Who is an administrator comes from Home Assistant's user list (`config/auth/list`),
asked for with the add-on's own Supervisor token and kept for five minutes.

With no ingress headers (running outside Home Assistant, e.g. in Docker for development)
there is no one to check, so changes are allowed, as before. If Home Assistant's user list
can't be read, changes are also allowed, rather than locking everyone out, and this is
logged.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

USERS_TTL = 300
_lock = threading.Lock()
_cache: dict[str, object] = {"at": 0.0, "users": None}


@dataclass(frozen=True)
class Visitor:
    id: str
    username: str
    display_name: str

    @property
    def label(self) -> str:
        return self.display_name or self.username or self.id


def visitor(headers) -> Visitor | None:
    uid = (headers.get("x-remote-user-id") or "").strip()
    if not uid:
        return None
    return Visitor(
        id=uid,
        username=(headers.get("x-remote-user-name") or "").strip(),
        display_name=(headers.get("x-remote-user-display-name") or "").strip(),
    )


def fetch_users(url: str, token: str) -> list[dict]:
    """Home Assistant's users: id, username, name, is_owner, group_ids…"""
    from websockets.sync.client import connect  # only needed for this lookup

    with connect(url, max_size=None, open_timeout=10) as ws:
        json.loads(ws.recv(timeout=10))  # "auth_required"
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(ws.recv(timeout=10)).get("type") != "auth_ok":
            raise RuntimeError("Home Assistant rejected the access token")
        ws.send(json.dumps({"id": 1, "type": "config/auth/list"}))
        while True:
            reply = json.loads(ws.recv(timeout=15))
            if reply.get("id") == 1 and reply.get("type") == "result":
                break
    if not reply.get("success"):
        raise RuntimeError((reply.get("error") or {}).get("message") or "user list refused")
    return list(reply.get("result") or [])


def users(settings) -> list[dict] | None:
    """The user list, cached; None when it can't be read."""
    with _lock:
        if _cache["users"] is not None and time.monotonic() - float(_cache["at"]) < USERS_TTL:
            return _cache["users"]  # type: ignore[return-value]
    if not (settings.ha_websocket_url and settings.ha_token):
        return None
    try:
        found = fetch_users(settings.ha_websocket_url, settings.ha_token)
    except Exception as exc:  # noqa: BLE001 - any failure means "can't tell"
        log.warning("Couldn't read Home Assistant's users to check who may change things: %s", exc)
        return None
    with _lock:
        _cache.update(at=time.monotonic(), users=found)
    return found


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def decide(settings, who: Visitor | None, user_list: list[dict] | None) -> tuple[bool, str]:
    """(may change, why) for this visitor. Pure, so it can be tested without HA."""
    if who is None:
        return True, "not opened through Home Assistant"
    named = {_norm(n) for n in (settings.edit_users or ()) if str(n).strip()}
    if named & {_norm(who.id), _norm(who.username), _norm(who.display_name)} - {""}:
        return True, "named in edit_users"
    mode = settings.edit_access
    if mode == "everyone":
        return True, "everyone may change things"
    if user_list is None:
        return True, "couldn't read Home Assistant's users"
    user = next((u for u in user_list if u.get("id") == who.id), None)
    if user is None:
        return False, "not a known Home Assistant user"
    if user.get("is_owner"):
        return True, "owner"
    if mode == "admins" and "system-admin" in (user.get("group_ids") or []):
        return True, "administrator"
    return False, "owner only" if mode == "owner" else "not an administrator"


def can_change(settings, headers) -> tuple[bool, str, Visitor | None]:
    who = visitor(headers)
    needs_list = who is not None and settings.edit_access != "everyone"
    ok, why = decide(settings, who, users(settings) if needs_list else None)
    return ok, why, who
