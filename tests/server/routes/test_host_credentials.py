"""Tests for the host-facing, provider-generic credential endpoint."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.connections.github import GithubConnectionStore
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.server.github_app import GitHubTokenSet
from omnigent.server.routes.host_credentials import create_host_credentials_router


class SecretBox:  # test double for the KMS SecretCipher: key- and context-bound
    def __init__(self, key: str) -> None:
        self._key = key

    def encrypt(self, plaintext: str, *, context) -> str:
        import base64
        import json

        return base64.b64encode(
            json.dumps({"k": self._key, "c": dict(context), "p": plaintext}).encode()
        ).decode("ascii")

    def decrypt(self, ciphertext: str, *, context):
        import base64
        import json

        try:
            d = json.loads(base64.b64decode(ciphertext.encode("ascii")))
        except ValueError:
            return None
        return d["p"] if d["k"] == self._key and d["c"] == dict(context) else None


@dataclass
class _Managed:
    user_id: str


class _FakeHostStore:
    """Resolves a single (host_id, token) pair to an owner and its bound sessions."""

    def __init__(
        self, host_id: str, token: str, owner: str, sessions: tuple[str, ...] = ()
    ) -> None:
        self._host_id, self._token, self._owner = host_id, token, owner
        self._sessions = sessions

    def resolve_launch_token(self, host_id: str, token: str) -> _Managed | None:
        if host_id == self._host_id and token == self._token:
            return _Managed(self._owner)
        return None

    def bound_session_ids(self, host_id: str) -> list[str]:
        return list(self._sessions) if host_id == self._host_id else []


@dataclass
class _Conversation:
    labels: dict[str, str]


class _FakeConversationStore:
    """Serves fixed labels per session id; ``None`` labels simulate a lookup fault."""

    def __init__(self, labels_by_session: dict[str, dict[str, str] | None]) -> None:
        self._labels = labels_by_session

    def get_conversation(self, conversation_id: str) -> _Conversation | None:
        if conversation_id not in self._labels:
            return None
        labels = self._labels[conversation_id]
        if labels is None:
            raise RuntimeError("conversation db down")
        return _Conversation(labels)


class _BoomStore:
    """A connection store whose reads raise — to prove the route degrades."""

    def get(self, *args, **kwargs):
        raise RuntimeError("db down")


def _app(host_store: _FakeHostStore, *, github_store, conversation_store=None) -> TestClient:
    app = FastAPI()
    # The generic route reads app.state.<provider>_{store,client}, populated by
    # the connection-provider wiring in create_app.
    app.state.github_store = github_store
    app.state.github_client = None
    app.include_router(
        create_host_credentials_router(host_store, conversation_store),  # type: ignore[arg-type]
        prefix="/v1",
    )
    return TestClient(app)


_HDR = {MANAGED_HOST_TOKEN_HEADER: "launch-tok"}


def test_returns_github_credential_for_valid_host_token(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    store = GithubConnectionStore(db_uri, SecretBox("enc-secret"))
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_live", "ghr_x", None, None, "repo"),
    )
    tc = _app(hs, github_store=store)
    resp = tc.get("/v1/hosts/host1/credentials/github", headers=_HDR)
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.json() == {
        "connected": True,
        "owner": "alice@example.com",
        "username": "x-access-token",
        "token": "ghu_live",
        "login": "octocat",
    }


def test_unauthenticated_without_or_with_bad_token(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    store = GithubConnectionStore(db_uri, SecretBox("enc-secret"))
    tc = _app(hs, github_store=store)
    assert tc.get("/v1/hosts/host1/credentials/github").status_code == 401
    bad = tc.get("/v1/hosts/host1/credentials/github", headers={MANAGED_HOST_TOKEN_HEADER: "nope"})
    assert bad.status_code == 401
    # Right token but wrong host id → also fails closed.
    assert tc.get("/v1/hosts/other/credentials/github", headers=_HDR).status_code == 401


def test_connected_false_when_owner_has_no_github(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    store = GithubConnectionStore(db_uri, SecretBox("enc-secret"))  # no upsert
    tc = _app(hs, github_store=store)
    resp = tc.get("/v1/hosts/host1/credentials/github", headers=_HDR)
    assert resp.status_code == 200
    assert resp.json() == {"connected": False}


def test_unknown_provider_is_404_but_only_after_auth(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    store = GithubConnectionStore(db_uri, SecretBox("enc-secret"))
    tc = _app(hs, github_store=store)
    # Authenticated, but no resolver/store registered for 'gitlab'.
    assert tc.get("/v1/hosts/host1/credentials/gitlab", headers=_HDR).status_code == 404
    # Unauthenticated stays 401 even for an unknown provider — auth is checked
    # first, so the endpoint reveals nothing about which providers exist.
    assert tc.get("/v1/hosts/host1/credentials/gitlab").status_code == 401


def test_resolver_fault_degrades_to_connected_false() -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com")
    tc = _app(hs, github_store=_BoomStore())
    resp = tc.get("/v1/hosts/host1/credentials/github", headers=_HDR)
    assert resp.status_code == 200
    assert resp.json() == {"connected": False}


def _connected_github_store(db_uri: str) -> GithubConnectionStore:
    store = GithubConnectionStore(db_uri, SecretBox("enc-secret"))
    store.upsert(
        "alice@example.com",
        github_login="octocat",
        github_user_id=42,
        tokens=GitHubTokenSet("ghu_live", "ghr_x", None, None, "repo"),
    )
    return store


def test_design_review_sandboxes_are_refused_every_provider(db_uri: str) -> None:
    store = _connected_github_store(db_uri)
    for labels in (
        {"omnigent.egm.cloud": "true", "omnigent.egm.commit": "abc"},
        {"omnigent.egm.coding.approval": "appr_1"},
    ):
        hs = _FakeHostStore("host1", "launch-tok", "alice@example.com", ("plain", "review"))
        conversations = _FakeConversationStore({"plain": {"team": "x"}, "review": labels})
        tc = _app(hs, github_store=store, conversation_store=conversations)
        for provider in ("github", "databricks", "gitlab"):
            resp = tc.get(f"/v1/hosts/host1/credentials/{provider}", headers=_HDR)
            assert resp.status_code == 403
            assert "ghu_live" not in resp.text
        # Authentication still comes first.
        assert tc.get("/v1/hosts/host1/credentials/github").status_code == 401


def test_ordinary_managed_sessions_still_receive_credentials(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com", ("plain", "gone"))
    conversations = _FakeConversationStore({"plain": {"omnigent.other": "1"}})
    tc = _app(hs, github_store=_connected_github_store(db_uri), conversation_store=conversations)
    resp = tc.get("/v1/hosts/host1/credentials/github", headers=_HDR)
    assert resp.status_code == 200
    assert resp.json()["token"] == "ghu_live"


def test_session_lookup_fault_fails_closed(db_uri: str) -> None:
    hs = _FakeHostStore("host1", "launch-tok", "alice@example.com", ("broken",))
    conversations = _FakeConversationStore({"broken": None})
    tc = _app(hs, github_store=_connected_github_store(db_uri), conversation_store=conversations)
    resp = tc.get("/v1/hosts/host1/credentials/github", headers=_HDR)
    assert resp.status_code == 503
    assert "ghu_live" not in resp.text
