"""
Authentication tools module for Kroger MCP server

This module provides OAuth authentication tools designed specifically for MCP context,
where the browser-based authentication flow needs to be handled through user interaction
rather than automated browser opening.
"""

import os
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from fastmcp import Context
from kroger_api import KrogerAPI

# Import the PKCE utilities from kroger-api
from kroger_api.utils import generate_pkce_parameters

from kroger_mcp.durable_token_store import persist_token_info

# Load environment variables
load_dotenv()

# Store PKCE parameters between steps
OAUTH_SCOPES = "product.compact cart.basic:write profile.compact"

_pkce_params = None
_auth_state = None


def register_auth_tools(mcp):
    """Register authentication-specific tools with the FastMCP server"""

    @mcp.tool()
    async def start_authentication(ctx: Context = None) -> Dict[str, Any]:
        """
        Start the OAuth authentication flow with Kroger.

        This tool returns a URL that the user needs to open in their browser
        to authenticate with Kroger. After authorization, the user will be
        redirected to a callback URL that they need to copy and paste back.

        Returns:
            Dictionary with authorization URL and instructions
        """
        global _pkce_params, _auth_state

        # Generate PKCE parameters
        _pkce_params = generate_pkce_parameters()

        # Generate a state parameter for CSRF protection
        _auth_state = _pkce_params.get("state", _pkce_params.get("code_verifier")[:16])

        # Get client_id and redirect_uri from environment
        client_id = os.environ.get("KROGER_CLIENT_ID")
        redirect_uri = os.environ.get(
            "KROGER_REDIRECT_URI", "http://localhost:8000/callback"
        )

        if not client_id:
            if ctx:
                await ctx.error("Missing KROGER_CLIENT_ID environment variable")
            return {
                "error": True,
                "message": "Missing KROGER_CLIENT_ID environment variable. Please set up your Kroger API credentials.",
            }

        # Initialize the Kroger API client
        kroger = KrogerAPI()

        # The kroger-api validator checks /profile, so profile.compact must be included.
        # Without it, a valid cart token receives a non-200 profile response and the
        # upstream library unnecessarily refreshes the token on validation.
        scopes = OAUTH_SCOPES

        # Get the authorization URL with PKCE
        auth_url = kroger.authorization.get_authorization_url(
            scope=scopes,
            state=_auth_state,
            code_challenge=_pkce_params["code_challenge"],
            code_challenge_method=_pkce_params["code_challenge_method"],
        )

        if ctx:
            await ctx.info(f"Generated auth URL with PKCE: {auth_url}")

        return {
            "auth_url": auth_url,
            "instructions": (
                "1. Click this link to authorize: [🔗 Authorize Kroger Access]({auth_url})\n"
                "   - Please present the authorization URL as a clickable markdown link\n"
                "2. Log in to your Kroger account and authorize the application\n"
                "3. After authorization, you'll be redirected to a callback URL\n"
                "4. Copy the FULL redirect URL from your browser's address bar\n"
                "5. Use the complete_authentication tool with that URL to complete the process"
            ).format(auth_url=auth_url),
        }

    @mcp.tool()
    async def complete_authentication(
        redirect_url: str, ctx: Context = None
    ) -> Dict[str, Any]:
        """
        Complete the OAuth flow using the redirect URL from Kroger.

        After opening the auth URL in your browser and authorizing the app,
        you'll be redirected to a callback URL. Copy that entire URL and
        pass it to this tool to complete the authentication process.

        Args:
            redirect_url: The full URL from your browser after authorization

        Returns:
            Dictionary indicating authentication status and whether the complete
            token payload was persisted to durable Upstash storage.
        """
        global _pkce_params, _auth_state

        if not _pkce_params or not _auth_state:
            if ctx:
                await ctx.error("Authentication flow not started")
            return {
                "error": True,
                "message": "Authentication flow not started. Please use start_authentication first.",
            }

        try:
            # Parse the redirect URL
            parsed_url = urlparse(redirect_url)
            query_params = parse_qs(parsed_url.query)

            # Extract code and state
            if "code" not in query_params:
                if ctx:
                    await ctx.error("Authorization code not found in redirect URL")
                return {
                    "error": True,
                    "message": "Authorization code not found in redirect URL. Please check the URL and try again.",
                }

            auth_code = query_params["code"][0]
            received_state = query_params.get("state", [None])[0]

            # Verify state parameter to prevent CSRF attacks
            if received_state != _auth_state:
                if ctx:
                    await ctx.error(
                        f"State mismatch: expected {_auth_state}, got {received_state}"
                    )
                return {
                    "error": True,
                    "message": "State parameter mismatch. This could indicate a CSRF attack. Please try authenticating again.",
                }

            # Get client credentials
            client_id = os.environ.get("KROGER_CLIENT_ID")
            client_secret = os.environ.get("KROGER_CLIENT_SECRET")
            redirect_uri = os.environ.get(
                "KROGER_REDIRECT_URI", "http://localhost:8000/callback"
            )

            if not client_id or not client_secret:
                if ctx:
                    await ctx.error("Missing Kroger API credentials")
                return {
                    "error": True,
                    "message": "Missing Kroger API credentials. Please set KROGER_CLIENT_ID and KROGER_CLIENT_SECRET.",
                }

            # Initialize Kroger API client
            kroger = KrogerAPI()

            # Exchange the authorization code for tokens with the code verifier
            if ctx:
                await ctx.info(
                    "Exchanging authorization code for tokens with code_verifier"
                )

            token_info = kroger.authorization.get_token_with_authorization_code(
                auth_code, code_verifier=_pkce_params["code_verifier"]
            )

            issued_refresh_token = token_info.get("refresh_token")
            durable_persistence = (
                persist_token_info(
                    token_info,
                    source="complete_authentication",
                )
                if issued_refresh_token
                else {
                    "configured": False,
                    "persisted": False,
                    "reason": "no_refresh_token",
                }
            )

            # Clear PKCE parameters and state after successful exchange
            _pkce_params = None
            _auth_state = None

            if ctx:
                await ctx.info("Authentication successful!")
                if durable_persistence.get("persisted"):
                    await ctx.info("Persisted the new Kroger token to Upstash.")
                else:
                    await ctx.warning(
                        "OAuth succeeded, but Upstash durable token storage is not configured. "
                        "The token will not survive a Render cold start."
                    )

            message = "Authentication successful! You can now use Kroger API tools that require authentication."
            if durable_persistence.get("persisted"):
                message += " The complete token payload was persisted to Upstash for cold-start recovery."
            else:
                message += (
                    " Upstash durable storage is not configured, so this token will be lost "
                    "when an ephemeral host restarts."
                )

            return {
                "success": True,
                "message": message,
                "durable_token_storage": durable_persistence,
                "token_info": {
                    "expires_in": token_info.get("expires_in"),
                    "token_type": token_info.get("token_type"),
                    "scope": token_info.get("scope"),
                    "has_refresh_token": "refresh_token" in token_info,
                },
            }

        except Exception as e:
            error_message = str(e)

            if ctx:
                await ctx.error(f"Authentication error: {error_message}")

            return {"error": True, "message": f"Authentication failed: {error_message}"}
