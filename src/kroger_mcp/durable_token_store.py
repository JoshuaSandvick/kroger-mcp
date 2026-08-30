"""Durable Kroger user-token storage backed by Upstash Redis REST.

Render's free web-service filesystem is erased on every spin-down. Render
environment-variable API updates are also not applied to an already-deployed
service, so they cannot be used as mutable token storage. This module keeps the
complete Kroger token payload in Upstash and provides a distributed refresh lock
for overlapping Render instances.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import requests

TOKEN_KEY = "kroger:user-token:v1"
REFRESH_LOCK_KEY = "kroger:user-token:refresh-lock:v1"
UPSTASH_URL_ENV = "UPSTASH_REDIS_REST_URL"
UPSTASH_TOKEN_ENV = "UPSTASH_REDIS_REST_TOKEN"
UPDATED_AT_FIELD = "_kroger_mcp_updated_at_ns"


class DurableTokenStoreError(RuntimeError):
    """Raised when configured durable token storage cannot be used safely."""


def token_fingerprint(token: str | None) -> str:
    """Return a safe, non-reversible identifier for token diagnostics."""
    if not token:
        return "none"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


class UpstashTokenStore:
    """Minimal Upstash Redis REST client for one Kroger token payload."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token

    @classmethod
    def from_env(cls) -> UpstashTokenStore | None:
        url = os.environ.get(UPSTASH_URL_ENV, "").strip()
        token = os.environ.get(UPSTASH_TOKEN_ENV, "").strip()
        if not url and not token:
            return None
        if not url or not token:
            raise DurableTokenStoreError(
                f"{UPSTASH_URL_ENV} and {UPSTASH_TOKEN_ENV} must either both be set "
                "or both be omitted."
            )
        return cls(url, token)

    def _command(self, *parts: object) -> Any:
        """Execute one Redis command with bounded retries.

        Command arguments are sent in the request body so token values never
        appear in URLs or logs.
        """
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = requests.post(
                    self.url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                    },
                    json=list(parts),
                    timeout=5,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("error"):
                    raise DurableTokenStoreError(
                        f"Upstash command failed: {payload['error']}"
                    )
                return payload.get("result")
            except (
                requests.RequestException,
                ValueError,
                DurableTokenStoreError,
            ) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.2 * (2**attempt))

        raise DurableTokenStoreError(
            f"Upstash durable token storage is unavailable: {last_error}"
        ) from last_error

    def load_token_info(self) -> dict[str, Any] | None:
        encoded = self._command("GET", TOKEN_KEY)
        if encoded is None:
            return None
        try:
            token_info = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DurableTokenStoreError(
                "The durable Kroger token payload is not valid JSON."
            ) from exc
        if not isinstance(token_info, dict) or not token_info.get("refresh_token"):
            raise DurableTokenStoreError(
                "The durable Kroger token payload has no refresh token."
            )
        return token_info

    def save_token_info(self, token_info: dict[str, Any]) -> None:
        refresh_token = token_info.get("refresh_token") if token_info else None
        if not refresh_token:
            raise DurableTokenStoreError(
                "Refusing to persist a Kroger user token without a refresh token."
            )
        encoded = json.dumps(token_info, separators=(",", ":"), sort_keys=True)
        result = self._command("SET", TOKEN_KEY, encoded)
        if result != "OK":
            raise DurableTokenStoreError(
                f"Upstash did not confirm the token write (result={result!r})."
            )

    @contextmanager
    def refresh_lock(
        self,
        *,
        wait_timeout_seconds: float = 15,
        lease_seconds: int = 30,
    ) -> Iterator[None]:
        """Serialize rotating refresh-token exchanges across service instances."""
        owner = uuid4().hex
        deadline = time.monotonic() + wait_timeout_seconds

        while True:
            acquired = self._command(
                "SET",
                REFRESH_LOCK_KEY,
                owner,
                "NX",
                "EX",
                lease_seconds,
            )
            # If the SET response was lost after being applied, recognize our own
            # owner value rather than waiting for our lease to expire.
            if acquired == "OK" or self._command("GET", REFRESH_LOCK_KEY) == owner:
                break
            if time.monotonic() >= deadline:
                raise DurableTokenStoreError(
                    "Timed out waiting for the distributed Kroger refresh lock."
                )
            time.sleep(0.1)

        try:
            yield
        finally:
            release_script = (
                'if redis.call("get", KEYS[1]) == ARGV[1] then '
                'return redis.call("del", KEYS[1]) else return 0 end'
            )
            try:
                self._command("EVAL", release_script, 1, REFRESH_LOCK_KEY, owner)
            except DurableTokenStoreError as exc:
                # The lease expires automatically; do not hide a completed Kroger
                # refresh merely because best-effort early release failed.
                print(
                    f"Warning: Could not release distributed Kroger refresh lock: {exc}",
                    file=sys.stderr,
                )


def get_token_store() -> UpstashTokenStore | None:
    """Return the configured Upstash store, or None for local-only operation."""
    return UpstashTokenStore.from_env()


def persist_token_info(
    token_info: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    """Persist a complete Kroger token payload when Upstash is configured."""
    store = get_token_store()
    refresh_token = token_info.get("refresh_token") if token_info else None
    fingerprint = token_fingerprint(refresh_token)
    if store is None:
        return {
            "configured": False,
            "persisted": False,
            "reason": "upstash_not_configured",
            "fingerprint": fingerprint,
        }

    store.save_token_info(token_info)
    print(
        f"Persisted Kroger token to Upstash source={source} "
        f"fingerprint={fingerprint}.",
        file=sys.stderr,
    )
    return {
        "configured": True,
        "persisted": True,
        "fingerprint": fingerprint,
    }
