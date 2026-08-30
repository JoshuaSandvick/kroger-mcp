import pytest

import bootstrap_tokens
from kroger_mcp import durable_token_store


def test_store_requires_both_upstash_environment_values(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://example.upstash.io")
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)

    with pytest.raises(durable_token_store.DurableTokenStoreError):
        durable_token_store.get_token_store()


def test_token_payload_round_trip(monkeypatch):
    store = durable_token_store.UpstashTokenStore("https://example", "secret")
    commands = []
    stored = {}

    def command(*parts):
        commands.append(parts)
        if parts[0] == "SET":
            stored[parts[1]] = parts[2]
            return "OK"
        if parts[0] == "GET":
            return stored.get(parts[1])
        raise AssertionError(parts)

    monkeypatch.setattr(store, "_command", command)
    token_info = {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_in": 1800,
        "token_type": "bearer",
    }

    store.save_token_info(token_info)

    assert store.load_token_info() == token_info
    assert commands[0][0:2] == ("SET", durable_token_store.TOKEN_KEY)


def test_cold_start_prefers_durable_token_over_stale_environment(monkeypatch):
    durable_token = {
        "access_token": "durable-access",
        "refresh_token": "durable-refresh",
        "expires_in": 1800,
        "token_type": "bearer",
    }
    writes = []

    class Store:
        def load_token_info(self):
            return durable_token

    monkeypatch.setattr(bootstrap_tokens, "get_token_store", lambda: Store())
    monkeypatch.setattr(bootstrap_tokens, "load_token", lambda token_file: None)
    monkeypatch.setattr(
        bootstrap_tokens,
        "save_token",
        lambda token_info, token_file: writes.append((token_info, token_file)),
    )
    monkeypatch.setenv("KROGER_USER_REFRESH_TOKEN", "stale-environment-refresh")

    bootstrap_tokens.bootstrap_user_token()

    assert writes == [(durable_token, bootstrap_tokens.TOKEN_FILE)]


def test_empty_store_is_bootstrapped_from_environment(monkeypatch):
    durable_writes = []
    local_writes = []

    class Store:
        def load_token_info(self):
            return None

        def save_token_info(self, token_info):
            durable_writes.append(token_info)

    monkeypatch.setattr(bootstrap_tokens, "get_token_store", lambda: Store())
    monkeypatch.setattr(bootstrap_tokens, "load_token", lambda token_file: None)
    monkeypatch.setattr(
        bootstrap_tokens,
        "save_token",
        lambda token_info, token_file: local_writes.append(token_info),
    )
    monkeypatch.setenv("KROGER_USER_REFRESH_TOKEN", "bootstrap-refresh")

    bootstrap_tokens.bootstrap_user_token()

    assert durable_writes == local_writes
    assert durable_writes[0]["refresh_token"] == "bootstrap-refresh"


def test_distributed_lock_releases_only_its_owner(monkeypatch):
    store = durable_token_store.UpstashTokenStore("https://example", "secret")
    commands = []

    def command(*parts):
        commands.append(parts)
        if parts[0] == "SET":
            return "OK"
        if parts[0] == "EVAL":
            return 1
        raise AssertionError(parts)

    monkeypatch.setattr(store, "_command", command)

    with store.refresh_lock():
        pass

    assert commands[0][0:2] == ("SET", durable_token_store.REFRESH_LOCK_KEY)
    assert commands[-1][0] == "EVAL"
    assert commands[-1][-2] == durable_token_store.REFRESH_LOCK_KEY
