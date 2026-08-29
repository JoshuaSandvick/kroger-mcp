"""
Shared utilities and client management for Kroger MCP server
"""

import os
import sys
import json
from typing import Optional, Dict, Any
from dotenv import load_dotenv

from kroger_api.kroger_api import KrogerAPI
from kroger_api.utils.env import load_and_validate_env, get_zip_code
from kroger_api.token_storage import load_token, get_token_file_path
from kroger_mcp.render_token_sync import sync_token_info_to_render, token_fingerprint

# Load environment variables
load_dotenv()

# Global state for clients and preferred location
_authenticated_client: Optional[KrogerAPI] = None
_client_credentials_client: Optional[KrogerAPI] = None

# JSON files for configuration storage
PREFERENCES_FILE = "kroger_preferences.json"
USER_TOKEN_FILE = ".kroger_token_user.json"


def get_client_credentials_client() -> KrogerAPI:
    """Get or create a client credentials authenticated client for public data"""
    global _client_credentials_client
    
    if _client_credentials_client is not None and _client_credentials_client.test_current_token():
        return _client_credentials_client
    
    _client_credentials_client = None
    
    try:
        load_and_validate_env(["KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET"])
        _client_credentials_client = KrogerAPI()
        
        # Try to load existing token first
        token_file = ".kroger_token_client_product.compact.json"
        token_info = load_token(token_file)
        
        if token_info:
            # Test if the token is still valid
            _client_credentials_client.client.token_info = token_info
            if _client_credentials_client.test_current_token():
                # Token is valid, use it
                return _client_credentials_client
        
        # Token is invalid or not found, get a new one
        token_info = _client_credentials_client.authorization.get_token_with_client_credentials("product.compact")
        return _client_credentials_client
    except Exception as e:
        raise Exception(f"Failed to get client credentials: {str(e)}")


def _client_from_user_token(token_info: Dict[str, Any]) -> Optional[KrogerAPI]:
    """Create and validate a user client from one token candidate.

    kroger-api's token validation already attempts a refresh when a refresh token
    is present. Returning None here means the candidate could not be validated or
    refreshed; callers may then try another candidate (for example the deployment
    seed token).
    """
    client = KrogerAPI()
    client.client.token_info = token_info
    client.client.token_file = USER_TOKEN_FILE

    try:
        before_refresh = token_info.get("refresh_token")
        print(
            f"Validating Kroger user token candidate "
            f"refresh={token_fingerprint(before_refresh)}.",
            file=sys.stderr,
        )

        if client.test_current_token():
            # test_current_token() may refresh internally through a kroger-api path
            # that does not call our KrogerClient.refresh_token monkey patch.
            # The client now holds the authoritative post-validation token payload,
            # so synchronize it regardless of which internal path was used.
            authoritative_token = client.client.token_info or token_info
            sync_result = sync_token_info_to_render(
                authoritative_token,
                source="post_test_current_token",
            )
            after_refresh = authoritative_token.get("refresh_token")
            print(
                f"Kroger user token validated "
                f"before={token_fingerprint(before_refresh)} "
                f"after={token_fingerprint(after_refresh)} "
                f"render_synced={sync_result.get('synced', False)}.",
                file=sys.stderr,
            )
            return client
    except Exception as exc:
        print(f"Kroger user token candidate failed: {exc}", file=sys.stderr)

    return None


def get_authenticated_client() -> KrogerAPI:
    """Get or create a user-authenticated client for cart operations.

    Token recovery order:
      1. Reuse the in-memory authenticated client without a proactive profile check.
      2. Try the token persisted by kroger-api.
      3. If configured and different, try KROGER_USER_REFRESH_TOKEN as a
         deployment-level recovery seed.

    The cached client is intentionally not validated here. kroger-api validates user
    tokens against the profile endpoint and refreshes on any non-200 response. That
    couples cart authentication to profile permissions/network health and previously
    caused needless refresh-token exchanges on normal cart calls. Actual Kroger API
    requests already perform reactive refresh on 401.

    This matters for remote MCP deployments because the token directory may be
    ephemeral. A stale on-disk token should not prevent a valid deployment seed
    from being tried.

    Returns:
        KrogerAPI: Authenticated client

    Raises:
        Exception: If no valid token is available and authentication is required
    """
    global _authenticated_client
    
    if _authenticated_client is not None:
        return _authenticated_client
    
    _authenticated_client = None
    
    try:
        load_and_validate_env(["KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET", "KROGER_REDIRECT_URI"])

        stored_token = load_token(USER_TOKEN_FILE)
        deployment_refresh_token = os.environ.get("KROGER_USER_REFRESH_TOKEN")
        if deployment_refresh_token:
            deployment_refresh_token = deployment_refresh_token.strip() or None
            print(
                f"Loaded Kroger deployment refresh-token seed "
                f"fingerprint={token_fingerprint(deployment_refresh_token)}.",
                file=sys.stderr,
            )

        candidates = []
        if stored_token:
            candidates.append(("stored", stored_token))

        stored_refresh_token = stored_token.get("refresh_token") if stored_token else None
        if deployment_refresh_token and deployment_refresh_token != stored_refresh_token:
            candidates.append((
                "deployment",
                {
                    "refresh_token": deployment_refresh_token,
                    "access_token": "",
                    "expires_in": 0,
                    "token_type": "bearer",
                },
            ))

        for source, token_info in candidates:
            client = _client_from_user_token(token_info)
            if client is not None:
                _authenticated_client = client
                if source == "deployment":
                    print(
                        "Recovered Kroger user authentication from KROGER_USER_REFRESH_TOKEN.",
                        file=sys.stderr,
                    )
                return _authenticated_client

        if deployment_refresh_token:
            raise Exception(
                "Authentication required. The configured KROGER_USER_REFRESH_TOKEN was present "
                "but could not be refreshed. It may be stale or revoked. Complete OAuth again, "
                "then update the deployment refresh-token seed before the next restart/deploy."
            )

        raise Exception(
            "Authentication required. No usable user token or KROGER_USER_REFRESH_TOKEN seed "
            "is available. Please use the start_authentication tool to begin the OAuth flow, "
            "then complete it with the complete_authentication tool."
        )
    except Exception as e:
        if "Authentication required" in str(e):
            raise
        raise Exception(f"Authentication failed: {str(e)}")


def invalidate_authenticated_client():
    """Invalidate the authenticated client to force re-authentication"""
    global _authenticated_client
    _authenticated_client = None


def invalidate_client_credentials_client():
    """Invalidate the client credentials client to force re-authentication"""
    global _client_credentials_client
    _client_credentials_client = None


def resolve_data_file(filename: str) -> str:
    """Resolve a state file to the shared per-user data directory.

    State files live alongside the OAuth tokens (see
    kroger_api.token_storage.get_token_file_path), NOT the current working
    directory: MCP hosts like Claude Desktop launch the server with a CWD
    that may be read-only and that varies between sessions, which made
    state silently fail to persist (issue #15). Any legacy copy found in
    the CWD is migrated once.
    """
    path = get_token_file_path(filename)
    if not os.path.exists(path) and os.path.exists(filename):
        try:
            with open(filename, 'r') as src, open(path, 'w') as dst:
                dst.write(src.read())
        except Exception as e:
            print(f"Warning: Could not migrate {filename} from CWD: {e}", file=sys.stderr)
    return path


def _load_preferences() -> dict:
    """Load preferences from file"""
    try:
        prefs_path = resolve_data_file(PREFERENCES_FILE)
        if os.path.exists(prefs_path):
            with open(prefs_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Warning: Could not load preferences: {e}", file=sys.stderr)
    return {"preferred_location_id": None}


def _save_preferences(preferences: dict) -> None:
    """Save preferences to file"""
    try:
        with open(resolve_data_file(PREFERENCES_FILE), 'w') as f:
            json.dump(preferences, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save preferences: {e}", file=sys.stderr)


def get_configured_location_id() -> Optional[str]:
    """Get the deployment-configured preferred location, if present."""
    location_id = os.getenv("KROGER_PREFERRED_LOCATION_ID")
    if not location_id:
        return None
    location_id = location_id.strip()
    return location_id or None


def get_preferred_location_id() -> Optional[str]:
    """Get the effective preferred location ID.

    A deployment-level KROGER_PREFERRED_LOCATION_ID is authoritative when set.
    This keeps remote MCP deployments deterministic even when their writable
    filesystem is ephemeral. Saved preferences remain as a fallback for local
    or interactive use.
    """
    configured_location_id = get_configured_location_id()
    if configured_location_id:
        return configured_location_id

    preferences = _load_preferences()
    return preferences.get("preferred_location_id")


def set_preferred_location_id(location_id: str) -> None:
    """Set the saved preferred location ID.

    Note: KROGER_PREFERRED_LOCATION_ID, when configured, remains authoritative.
    """
    preferences = _load_preferences()
    preferences["preferred_location_id"] = location_id
    _save_preferences(preferences)


def format_currency(value: Optional[float]) -> str:
    """Format a value as currency"""
    if value is None:
        return "N/A"
    return f"${value:.2f}"


def get_default_zip_code() -> str:
    """Get the default zip code from environment or fallback"""
    return get_zip_code(default="10001")
