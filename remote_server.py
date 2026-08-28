import os

from bootstrap_tokens import bootstrap_user_token
from kroger_mcp.server import create_server


def main():
    bootstrap_user_token()

    mcp = create_server()

    port = int(os.environ.get("PORT", "10000"))

    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=port,
        path="/mcp",
    )


if __name__ == "__main__":
    main()
