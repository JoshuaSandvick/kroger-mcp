import sys

from kroger_api.client import KrogerClient
from kroger_api.token_storage import save_token
from kroger_mcp.render_token_sync import sync_refresh_token_to_render, token_fingerprint


_original_refresh_token = KrogerClient.refresh_token


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
    save_token(token_info, token_file)

    self.token_info = token_info

    sync_result = sync_refresh_token_to_render(
        token_info["refresh_token"],
        source="patched_refresh_token",
    )

    print(
        "Kroger access token refreshed; refresh token preserved. "
        f"fingerprint={token_fingerprint(token_info.get('refresh_token'))}."
        + (" Render seed synchronized." if sync_result.get("synced") else ""),
        file=sys.stderr,
    )

    return token_info


def install_refresh_patch():
    KrogerClient.refresh_token = patched_refresh_token
    print("Installed Kroger refresh-token preservation patch.", file=sys.stderr)
