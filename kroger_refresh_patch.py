import sys
import threading
import time
from contextlib import nullcontext

import kroger_api.client as kroger_client_module
from kroger_api import token_storage
from kroger_api.client import KrogerClient

from kroger_mcp.durable_token_store import (
    UPDATED_AT_FIELD,
    get_token_store,
    persist_token_info,
    token_fingerprint,
)

_original_save_token = token_storage.save_token
_refresh_lock = threading.RLock()


def patched_save_token(token_info, token_file=None):
    """Persist tokens locally and mirror complete user-token payloads to Upstash.

    kroger-api imports save_token into kroger_api.client at module import time,
    so install_refresh_patch replaces both references. This makes persistence
    happen at the storage boundary regardless of which library path saved the token.
    """
    if token_file is None:
        _original_save_token(token_info)
    else:
        _original_save_token(token_info, token_file)

    refresh_token = token_info.get("refresh_token") if token_info else None
    if refresh_token:
        token_info.setdefault(UPDATED_AT_FIELD, time.time_ns())
        persist_token_info(
            token_info,
            source="token_storage_save",
        )


def patched_refresh_token(self, refresh_token: str):
    """
    Refresh Kroger user auth while preserving rotation safely.

    Refresh-token exchanges are serialized both within this process and, when
    Upstash is configured, across Render instances. Before exchanging, re-read the
    durable and local token payloads. If another request already refreshed, reuse
    its complete token payload instead of submitting the stale refresh token again.
    """
    token_file = self.token_file or ".kroger_token_user.json"

    store = get_token_store()

    with _refresh_lock:
        distributed_lock = store.refresh_lock() if store else nullcontext()
        with distributed_lock:
            durable_token_info = store.load_token_info() if store else None
            local_token_info = token_storage.load_token(token_file)

            current_access_token = (
                self.token_info.get("access_token") if self.token_info else None
            )
            token_candidates = []
            token_copies_differ = False
            for priority, (source, latest_token_info) in enumerate(
                (
                    ("durable", durable_token_info),
                    ("local", local_token_info),
                )
            ):
                if not latest_token_info:
                    continue
                latest_refresh_token = latest_token_info.get("refresh_token")
                latest_access_token = latest_token_info.get("access_token")
                refresh_changed = (
                    latest_refresh_token and latest_refresh_token != refresh_token
                )
                access_changed = (
                    latest_access_token and latest_access_token != current_access_token
                )
                token_copies_differ = (
                    token_copies_differ or refresh_changed or access_changed
                )
                # A local write can be newer than Upstash when Kroger rotated
                # a token but the durable write temporarily failed. The marker
                # lets the next request recover without reverting to the now-
                # invalid durable ancestor. Durable wins ties and legacy payloads.
                updated_at = latest_token_info.get(UPDATED_AT_FIELD, 0)
                token_candidates.append(
                    (updated_at, -priority, source, latest_token_info)
                )

            if token_copies_differ:
                _, _, source, latest_token_info = max(
                    token_candidates, key=lambda item: item[:2]
                )
                latest_refresh_token = latest_token_info.get("refresh_token")
                _original_save_token(latest_token_info, token_file)
                self.token_info = latest_token_info
                if store and source == "local":
                    store.save_token_info(latest_token_info)
                print(
                    "Skipped stale Kroger refresh-token exchange; a newer "
                    f"{source} token exists "
                    f"fingerprint={token_fingerprint(latest_refresh_token)}.",
                    file=sys.stderr,
                )
                return latest_token_info

            effective_refresh_token = (
                durable_token_info.get("refresh_token")
                if durable_token_info
                else refresh_token
            )
            token_info = self._get_token(
                grant_type="refresh_token",
                refresh_token=effective_refresh_token,
            )

            # Some OAuth servers do not return refresh_token on every refresh.
            # Preserve the token we used unless Kroger explicitly supplies a new one.
            if not token_info.get("refresh_token"):
                token_info["refresh_token"] = effective_refresh_token

            patched_save_token(token_info, token_file)
            self.token_info = token_info

            print(
                "Kroger access token refreshed and durably persisted. "
                f"fingerprint={token_fingerprint(token_info.get('refresh_token'))}.",
                file=sys.stderr,
            )

            return token_info


def install_refresh_patch():
    KrogerClient.refresh_token = patched_refresh_token
    token_storage.save_token = patched_save_token
    kroger_client_module.save_token = patched_save_token
    print(
        "Installed Kroger refresh-token preservation and Upstash storage patch.",
        file=sys.stderr,
    )
