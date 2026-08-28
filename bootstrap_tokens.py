import json
import os
from pathlib import Path


def bootstrap_user_token():
    refresh_token = os.environ.get("KROGER_USER_REFRESH_TOKEN")

    if not refresh_token:
        print("KROGER_USER_REFRESH_TOKEN is not set; skipping user token bootstrap.")
        return

    token_dir = Path(
        os.environ.get(
            "KROGER_TOKEN_DIR",
            str(Path.home() / ".local" / "share" / "kroger-mcp")
        )
    )

    token_dir.mkdir(parents=True, exist_ok=True)

    token_file = token_dir / ".kroger_token_user.json"

    # Only seed the file if it doesn't already exist.
    if token_file.exists():
        print(f"User token file already exists at {token_file}")
        return

    token_data = {
        "refresh_token": refresh_token,
        "access_token": "",
        "expires_in": 0,
        "token_type": "bearer"
    }

    token_file.write_text(json.dumps(token_data, indent=2))
    print(f"Seeded Kroger user refresh token at {token_file}")
