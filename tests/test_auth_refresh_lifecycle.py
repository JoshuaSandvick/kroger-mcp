from types import SimpleNamespace

from kroger_mcp.tools import auth
from kroger_mcp.tools import shared
import kroger_refresh_patch


def test_oauth_scope_includes_profile_for_upstream_validator():
    scopes = set(auth.OAUTH_SCOPES.split())
    assert "cart.basic:write" in scopes
    assert "profile.compact" in scopes


def test_cached_authenticated_client_is_reused_without_proactive_validation(monkeypatch):
    class CachedClient:
        def test_current_token(self):
            raise AssertionError("cached user client should not be proactively validated")

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
        token_info = {
            "access_token": "old-access-token",
            "refresh_token": old_refresh,
        }

        def _get_token(self, **kwargs):
            raise AssertionError("stale refresh token must not be exchanged")

    client = DummyClient()
    result = kroger_refresh_patch.patched_refresh_token(client, old_refresh)

    assert result == newer_token
    assert client.token_info == newer_token
