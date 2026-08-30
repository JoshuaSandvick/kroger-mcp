import os

from kroger_api.token_storage import load_token, save_token

from kroger_mcp.durable_token_store import get_token_store, token_fingerprint

TOKEN_FILE = ".kroger_token_user.json"


def bootstrap_user_token():
    """Hydrate kroger-api's local token file from durable storage.

    KROGER_USER_REFRESH_TOKEN is a one-time bootstrap fallback only. Once Upstash
    contains a token payload, it is authoritative across Render cold starts.
    """
    store = get_token_store()
    if store:
        durable_token = store.load_token_info()
        if durable_token:
            save_token(durable_token, TOKEN_FILE)
            print(
                "Seeded local Kroger token from Upstash "
                f"fingerprint={token_fingerprint(durable_token.get('refresh_token'))}."
            )
            return

    # Without Upstash, preserve the existing local-development behavior.
    existing = load_token(TOKEN_FILE)

    if existing and existing.get("refresh_token"):
        print("Existing Kroger user token found.")
        return

    refresh_token = os.environ.get("KROGER_USER_REFRESH_TOKEN")

    if not refresh_token:
        print("KROGER_USER_REFRESH_TOKEN is not set; " "skipping user token bootstrap.")
        return

    token_data = {
        "refresh_token": refresh_token,
        "access_token": "",
        "expires_in": 0,
        "token_type": "bearer",
    }

    save_token(token_data, TOKEN_FILE)

    if store:
        store.save_token_info(token_data)
        print(
            "Bootstrapped Upstash from KROGER_USER_REFRESH_TOKEN "
            f"fingerprint={token_fingerprint(refresh_token)}."
        )

    print("Seeded Kroger user token using kroger-api token storage.")
