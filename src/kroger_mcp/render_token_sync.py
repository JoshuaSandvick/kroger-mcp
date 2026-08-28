"""Persist Kroger refresh-token rotation into Render service configuration.

Render free web services have ephemeral filesystems, so the local kroger-api token
file does not survive a restart. When configured with a Render API key, this
module updates the service-level KROGER_USER_REFRESH_TOKEN environment variable
without triggering a deploy, allowing the next instance to bootstrap from the
latest refresh token.
"""

import os
import sys
from typing import Dict, Any

import requests


RENDER_API_BASE = "https://api.render.com/v1"
TOKEN_ENV_KEY = "KROGER_USER_REFRESH_TOKEN"


def sync_refresh_token_to_render(refresh_token: str) -> Dict[str, Any]:
    """Persist a refresh token into this Render service's environment settings.

    This is intentionally opt-in. If the process is not running on Render, or if
    RENDER_API_KEY is not configured, no external call is made.
    """
    if not refresh_token:
        return {"attempted": False, "synced": False, "reason": "no_refresh_token"}

    if os.environ.get("RENDER", "").lower() != "true":
        return {"attempted": False, "synced": False, "reason": "not_render"}

    api_key = os.environ.get("RENDER_API_KEY")
    service_id = os.environ.get("RENDER_SERVICE_ID")
    if not api_key or not service_id:
        return {
            "attempted": False,
            "synced": False,
            "reason": "render_api_not_configured",
        }

    url = f"{RENDER_API_BASE}/services/{service_id}/env-vars/{TOKEN_ENV_KEY}"

    try:
        response = requests.put(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"value": refresh_token},
            timeout=10,
        )
        response.raise_for_status()

        # Keep this process consistent with the durable seed we just stored.
        os.environ[TOKEN_ENV_KEY] = refresh_token
        print(
            "Synchronized Kroger refresh token to Render service environment.",
            file=sys.stderr,
        )
        return {"attempted": True, "synced": True}
    except Exception as exc:
        # Never log the token itself.
        print(
            f"Warning: Could not synchronize Kroger refresh token to Render: {exc}",
            file=sys.stderr,
        )
        return {
            "attempted": True,
            "synced": False,
            "reason": "render_api_error",
            "error": str(exc),
        }
