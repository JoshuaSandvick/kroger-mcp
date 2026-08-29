"""Persist Kroger refresh-token rotation into Render service configuration.

Render free web services have ephemeral filesystems, so the local kroger-api token
file does not survive a restart. Whenever Kroger issues or rotates a refresh token,
this module can update the service-level KROGER_USER_REFRESH_TOKEN environment
variable without triggering a deploy.
"""

import hashlib
import os
import sys
from typing import Dict, Any, Optional

import requests


RENDER_API_BASE = "https://api.render.com/v1"
TOKEN_ENV_KEY = "KROGER_USER_REFRESH_TOKEN"


def token_fingerprint(token: Optional[str]) -> str:
    """Return a safe, non-reversible identifier for token diagnostics."""
    if not token:
        return "none"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def _render_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _resolve_render_service_id(api_key: str) -> Optional[str]:
    """Resolve the current Render service ID without relying on undocumented env vars."""
    configured = os.environ.get("RENDER_SERVICE_ID")
    if configured:
        configured = configured.strip()
        if configured:
            return configured

    service_name = os.environ.get("RENDER_SERVICE_NAME")
    if not service_name:
        return None

    service_name = service_name.strip()
    if not service_name:
        return None

    response = requests.get(
        f"{RENDER_API_BASE}/services",
        headers=_render_headers(api_key),
        params={"name": service_name, "limit": 100},
        timeout=10,
    )
    response.raise_for_status()
    matches = []
    for item in response.json():
        service = item.get("service", item)
        if service.get("name") == service_name and service.get("id"):
            matches.append(service["id"])

    if len(matches) == 1:
        return matches[0]

    print(
        f"Warning: Could not uniquely resolve Render service "
        f"name={service_name!r}; matches={len(matches)}.",
        file=sys.stderr,
    )
    return None


def sync_refresh_token_to_render(
    refresh_token: str,
    *,
    source: str = "unknown",
) -> Dict[str, Any]:
    """Persist a refresh token into this Render service's environment settings.

    The actual token is never logged. Diagnostics use a short SHA-256 fingerprint.
    If the process-local Render seed already matches the supplied token, no API call
    is needed.
    """
    if not refresh_token:
        return {"attempted": False, "synced": False, "reason": "no_refresh_token"}

    refresh_token = refresh_token.strip()
    if not refresh_token:
        return {"attempted": False, "synced": False, "reason": "no_refresh_token"}

    current_seed = os.environ.get(TOKEN_ENV_KEY)
    if current_seed:
        current_seed = current_seed.strip() or None

    new_fp = token_fingerprint(refresh_token)
    current_fp = token_fingerprint(current_seed)
    print(
        f"Kroger refresh-token sync check source={source} "
        f"current={current_fp} candidate={new_fp}.",
        file=sys.stderr,
    )

    if current_seed == refresh_token:
        return {
            "attempted": False,
            "synced": True,
            "reason": "already_current",
            "fingerprint": new_fp,
        }

    if os.environ.get("RENDER", "").lower() != "true":
        return {
            "attempted": False,
            "synced": False,
            "reason": "not_render",
            "fingerprint": new_fp,
        }

    api_key = os.environ.get("RENDER_API_KEY")
    if not api_key:
        return {
            "attempted": False,
            "synced": False,
            "reason": "render_api_not_configured",
            "fingerprint": new_fp,
        }

    try:
        service_id = _resolve_render_service_id(api_key)
        if not service_id:
            return {
                "attempted": True,
                "synced": False,
                "reason": "render_service_not_resolved",
                "fingerprint": new_fp,
            }

        url = f"{RENDER_API_BASE}/services/{service_id}/env-vars/{TOKEN_ENV_KEY}"
        response = requests.put(
            url,
            headers=_render_headers(api_key),
            json={"value": refresh_token},
            timeout=10,
        )
        response.raise_for_status()

        # Keep this process consistent with the durable seed we just stored.
        os.environ[TOKEN_ENV_KEY] = refresh_token
        print(
            f"Synchronized Kroger refresh token to Render source={source} "
            f"fingerprint={new_fp}.",
            file=sys.stderr,
        )
        return {
            "attempted": True,
            "synced": True,
            "fingerprint": new_fp,
        }
    except Exception as exc:
        # Never log the token itself.
        print(
            f"Warning: Could not synchronize Kroger refresh token to Render "
            f"source={source} fingerprint={new_fp}: {exc}",
            file=sys.stderr,
        )
        return {
            "attempted": True,
            "synced": False,
            "reason": "render_api_error",
            "error": str(exc),
            "fingerprint": new_fp,
        }


def sync_token_info_to_render(
    token_info: Optional[Dict[str, Any]],
    *,
    source: str,
) -> Dict[str, Any]:
    """Sync the refresh token contained in a Kroger token payload, if present."""
    if not token_info:
        return {"attempted": False, "synced": False, "reason": "no_token_info"}

    refresh_token = token_info.get("refresh_token")
    if not refresh_token:
        return {
            "attempted": False,
            "synced": False,
            "reason": "no_refresh_token",
        }

    return sync_refresh_token_to_render(refresh_token, source=source)
