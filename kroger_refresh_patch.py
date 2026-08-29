import sys

import kroger_api.client as kroger_client_module
import kroger_api.token_storage as token_storage
from kroger_api.client import KrogerClient
from kroger_mcp.render_token_sync import sync_refresh_token_to_render, token_fingerprint


_original_refresh_token = KrogerClient.refresh_token
_original_save_token = token_storage.save_token


def patched_save_token(token_info, token_file=None):
    """Persist tokens locally and mirror any user refresh token to Render.

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
        sync_refresh_token_to_render(
            refresh_token,
            source="token_storage_save",
        )


def patched_refresh_token(self, refresh_token: str):
    """
    Refresh Kroger user auth while preserving the existing refresh token
    if Kroger does not return one in the refresh response.

    On Render, also persist the effective refresh token to the service's
    KROGER_USER_REFRESH_TOKEN setting when Render API synchronization is enabled.
    """
    token_info = self._get_token(
        grant_type="refresh_token",
        refresh_token=refresh_token,
    )

    # Some OAuth servers do not return refresh_token on every refresh.
    # Preserve the token we used unless Kroger explicitly supplies a new one.
    if not token_info.get("refresh_token"):
        token_info["refresh_token"] = refresh_token

    # Save to the same user-token location kroger-api normally uses.
    token_file = self.token_file or ".kroger_token_user.json"
    patched_save_token(token_info, token_file)

    self.token_info = token_info

    print(
        "Kroger access token refreshed; refresh token preserved. "
        f"fingerprint={token_fingerprint(token_info.get('refresh_token'))}.",
        file=sys.stderr,
    )

    return token_info


def install_refresh_patch():
    KrogerClient.refresh_token = patched_refresh_token
    token_storage.save_token = patched_save_token
    kroger_client_module.save_token = patched_save_token
    print(
        "Installed Kroger refresh-token preservation and storage-sync patch.",
        file=sys.stderr,
    )
