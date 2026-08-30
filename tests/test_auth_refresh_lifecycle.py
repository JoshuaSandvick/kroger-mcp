from contextlib import contextmanager
from typing import ClassVar

import kroger_refresh_patch
from kroger_mcp.durable_token_store import UPDATED_AT_FIELD
from kroger_mcp.tools import auth, shared


class FakeRefreshStore:
    def __init__(self, token_info):
        self.token_info = token_info
        self.lock_entries = 0

    @contextmanager
    def refresh_lock(self):
        self.lock_entries += 1
        yield

    def load_token_info(self):
        return self.token_info


def test_oauth_scope_includes_profile_for_upstream_validator():
    scopes = set(auth.OAUTH_SCOPES.split())
    assert "cart.basic:write" in scopes
    assert "profile.compact" in scopes


def test_cached_authenticated_client_is_reused_without_proactive_validation(
    monkeypatch,
):
    class CachedClient:
        def test_current_token(self):
            raise AssertionError(
                "cached user client should not be proactively validated"
            )

    cached = CachedClient()
    monkeypatch.setattr(shared, "_authenticated_client", cached)

    assert shared.get_authenticated_client() is cached


def test_stale_refresh_attempt_reuses_newer_stored_token(monkeypatch):
    old_refresh = "old-refresh-token"
    newer_token = {
        "access_token": "new-access-token",
        "refresh_token": "new-refresh-token",
        "expires_in": 1800,
        "token_type": "bearer",
    }

    monkeypatch.setattr(
        kroger_refresh_patch.token_storage,
        "load_token",
        lambda token_file: newer_token,
    )

    class DummyClient:
        token_file = ".kroger_token_user.json"
        token_info: ClassVar = {
            "access_token": "old-access-token",
            "refresh_token": old_refresh,
        }

        def _get_token(self, **kwargs):
            raise AssertionError("stale refresh token must not be exchanged")

    client = DummyClient()
    result = kroger_refresh_patch.patched_refresh_token(client, old_refresh)

    assert result == newer_token
    assert client.token_info == newer_token


def test_stale_cross_instance_refresh_reuses_complete_durable_token(monkeypatch):
    durable_token = {
        "access_token": "durable-access-token",
        "refresh_token": "durable-refresh-token",
        "expires_in": 1800,
        "token_type": "bearer",
    }
    store = FakeRefreshStore(durable_token)
    local_writes = []

    monkeypatch.setattr(kroger_refresh_patch, "get_token_store", lambda: store)
    monkeypatch.setattr(
        kroger_refresh_patch.token_storage,
        "load_token",
        lambda token_file: {
            "access_token": "stale-access-token",
            "refresh_token": "stale-refresh-token",
        },
    )
    monkeypatch.setattr(
        kroger_refresh_patch,
        "_original_save_token",
        lambda token_info, token_file: local_writes.append(token_info),
    )

    class DummyClient:
        token_file = ".kroger_token_user.json"
        token_info: ClassVar = {
            "access_token": "stale-access-token",
            "refresh_token": "stale-refresh-token",
        }

        def _get_token(self, **kwargs):
            raise AssertionError("the stale token must never be exchanged")

    client = DummyClient()
    result = kroger_refresh_patch.patched_refresh_token(
        client,
        "stale-refresh-token",
    )

    assert result == durable_token
    assert client.token_info == durable_token
    assert local_writes == [durable_token]
    assert store.lock_entries == 1


def test_refresh_rotation_persists_complete_payload(monkeypatch):
    current_token = {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
    }
    rotated_token = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_in": 1800,
        "token_type": "bearer",
    }
    store = FakeRefreshStore(current_token)
    durable_writes = []
    local_writes = []

    monkeypatch.setattr(kroger_refresh_patch, "get_token_store", lambda: store)
    monkeypatch.setattr(
        kroger_refresh_patch.token_storage,
        "load_token",
        lambda token_file: current_token,
    )
    monkeypatch.setattr(
        kroger_refresh_patch,
        "_original_save_token",
        lambda token_info, token_file=None: local_writes.append(token_info),
    )
    monkeypatch.setattr(
        kroger_refresh_patch,
        "persist_token_info",
        lambda token_info, source: durable_writes.append((token_info, source)),
    )

    class DummyClient:
        token_file = ".kroger_token_user.json"
        token_info = current_token

        def _get_token(self, **kwargs):
            assert kwargs["refresh_token"] == "old-refresh"
            return rotated_token.copy()

    client = DummyClient()
    result = kroger_refresh_patch.patched_refresh_token(client, "old-refresh")

    assert result["access_token"] == rotated_token["access_token"]
    assert result["refresh_token"] == rotated_token["refresh_token"]
    assert result[UPDATED_AT_FIELD] > 0
    assert client.token_info == result
    assert local_writes == [result]
    assert durable_writes == [(result, "token_storage_save")]
    assert store.lock_entries == 1


def test_newer_local_token_repairs_stale_durable_copy(monkeypatch):
    durable_token = {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        UPDATED_AT_FIELD: 100,
    }
    local_token = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        UPDATED_AT_FIELD: 200,
    }
    store = FakeRefreshStore(durable_token)
    durable_writes = []
    store.save_token_info = durable_writes.append

    monkeypatch.setattr(kroger_refresh_patch, "get_token_store", lambda: store)
    monkeypatch.setattr(
        kroger_refresh_patch.token_storage,
        "load_token",
        lambda token_file: local_token,
    )
    monkeypatch.setattr(
        kroger_refresh_patch, "_original_save_token", lambda *args: None
    )

    class DummyClient:
        token_file = ".kroger_token_user.json"
        token_info = local_token

        def _get_token(self, **kwargs):
            raise AssertionError("the recovered local token must not be exchanged")

    result = kroger_refresh_patch.patched_refresh_token(
        DummyClient(),
        "new-refresh",
    )

    assert result == local_token
    assert durable_writes == [local_token]
