"""Viewers can look but not change: who may save or delete, from Home Assistant's ingress
headers and user list."""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from energy_tracker import access, api

OWNER = {
    "id": "u-nan",
    "username": "nan",
    "name": "Nan",
    "is_owner": True,
    "group_ids": ["system-admin"],
}
ADMIN = {
    "id": "u-ros",
    "username": "rosalyn",
    "name": "Rosalyn",
    "is_owner": False,
    "group_ids": ["system-admin"],
}
USER = {
    "id": "u-sum",
    "username": "summer",
    "name": "Summer",
    "is_owner": False,
    "group_ids": ["system-users"],
}
USERS = [OWNER, ADMIN, USER]


def headers(user):
    return {
        "X-Remote-User-Id": user["id"],
        "X-Remote-User-Name": user["username"],
        "X-Remote-User-Display-Name": user["name"],
    }


def with_settings(**changes):
    return dataclasses.replace(api.settings(), **changes)


def who(user):
    return access.visitor({k.lower(): v for k, v in headers(user).items()})


@pytest.mark.parametrize(
    ("mode", "named", "user", "expected"),
    [
        ("admins", (), OWNER, True),
        ("admins", (), ADMIN, True),
        ("admins", (), USER, False),
        ("owner", (), OWNER, True),
        ("owner", (), ADMIN, False),
        ("owner", ("Rosalyn",), ADMIN, True),  # display name
        ("owner", ("summer",), USER, True),  # username
        ("everyone", (), USER, True),
    ],
)
def test_decide(mode, named, user, expected):
    s = with_settings(edit_access=mode, edit_users=named)
    assert access.decide(s, who(user), USERS)[0] is expected


def test_no_ingress_headers_or_no_user_list_allows_changes():
    s = with_settings(edit_access="owner")
    assert access.decide(s, None, USERS)[0] is True  # running outside Home Assistant
    assert access.decide(s, who(USER), None)[0] is True  # couldn't ask Home Assistant
    assert access.decide(s, who({**USER, "id": "u-gone"}), USERS)[0] is False


def test_viewers_can_read_but_not_change(monkeypatch):
    fixed = with_settings(edit_access="admins", allowed_client=None)
    monkeypatch.setattr(api, "settings", lambda: fixed)
    monkeypatch.setattr(access, "users", lambda _s: USERS)
    client = TestClient(api.app)

    assert client.get("/api/access", headers=headers(USER)).json() == {
        "can_change": False,
        "why": "not an administrator",
        "user": "Summer",
    }
    assert client.get("/api/access", headers=headers(OWNER)).json()["can_change"] is True
    assert client.get("/api/tariffs", headers=headers(USER)).status_code == 200

    refused = client.post(
        "/api/notes", headers=headers(USER), json={"day": "2026-10-24", "text": "x"}
    )
    assert refused.status_code == 403 and "View only" in refused.json()["detail"]
    assert client.delete("/api/tariffs/2026-01-01", headers=headers(USER)).status_code == 403
    assert client.post("/api/layout", headers=headers(USER), json={"folded": []}).status_code == 403
    # The owner's change goes through to the endpoint (whatever it then says about the body)
    assert (
        client.post("/api/layout", headers=headers(OWNER), json={"folded": []}).status_code != 403
    )
