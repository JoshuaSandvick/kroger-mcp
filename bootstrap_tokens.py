import os

from kroger_api.token_storage import save_token, load_token


TOKEN_FILE = ".kroger_token_user.json"


def bootstrap_user_token():
    # If kroger-api already has a token, leave it alone.
    existing = load_token(TOKEN_FILE)

    if existing and existing.get("refresh_token"):
        print("Existing Kroger user token found.")
        return

    refresh_token = os.environ.get("KROGER_USER_REFRESH_TOKEN")

    if not refresh_token:
        print(
            "KROGER_USER_REFRESH_TOKEN is not set; "
            "skipping user token bootstrap."
        )
        return

    token_data = {
        "refresh_token": refresh_token,
        "access_token": "",
        "expires_in": 0,
        "token_type": "bearer",
    }

    save_token(token_data, TOKEN_FILE)

    print("Seeded Kroger user token using kroger-api token storage.")
