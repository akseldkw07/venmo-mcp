"""One-time Venmo auth script. Run with: pixi run auth"""

import json
from pathlib import Path

from venmo_api.apis.auth_api import AuthenticationApi
from venmo_api.utils.api_client import ApiClient

TOKEN_FILE = Path(__file__).parent / ".venmo-token"


def main():
    if TOKEN_FILE.exists():
        print(f"Token already exists at {TOKEN_FILE}")
        print("Delete it first if you want to re-authenticate.")
        return

    device_id = input("Device ID (see README for how to obtain): ").strip()
    if not device_id:
        print("A real device ID is required. Venmo rejects random ones.")
        return

    username = input("Venmo username (email or phone without +1): ")
    password = input("Venmo password: ")

    authn = AuthenticationApi(api_client=ApiClient(), device_id=device_id)
    access_token = authn.login_with_credentials_cli(
        username=username, password=password
    )

    TOKEN_FILE.write_text(
        json.dumps(
            {
                "access_token": access_token,
                "device_id": device_id,
            }
        )
    )
    TOKEN_FILE.chmod(0o600)
    print(f"Token and device-id saved to {TOKEN_FILE}")


if __name__ == "__main__":
    main()
