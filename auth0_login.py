#!/usr/bin/env python3
"""Auth0 Device Authorization Grant flow for k8scc.

User-facing prompts go to stderr (visible in the terminal).
On success, prints the user's email to stdout (captured by entrypoint.sh).
Requires: AUTH0_DOMAIN, AUTH0_CLIENT_ID env vars.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import base64

DOMAIN = os.environ["AUTH0_DOMAIN"]
CLIENT_ID = os.environ["AUTH0_CLIENT_ID"]
SCOPE = "openid email profile"


def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _decode_jwt_payload(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (4 - len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def main():
    # Step 1: request device code
    try:
        device = _post(
            f"https://{DOMAIN}/oauth/device/code",
            {"client_id": CLIENT_ID, "scope": SCOPE},
        )
    except Exception as e:
        print(f"無法連接 Auth0：{e}", file=sys.stderr)
        sys.exit(1)

    # Step 2: show user code
    print(file=sys.stderr)
    print(f"請前往以下網址完成授權：", file=sys.stderr)
    print(f"  {device['verification_uri_complete']}", file=sys.stderr)
    print(f"驗證碼：{device['user_code']}", file=sys.stderr)
    print("等待授權中...", file=sys.stderr, flush=True)

    # Step 3: poll for token
    interval = device.get("interval", 5)
    deadline = time.time() + device.get("expires_in", 300)

    while time.time() < deadline:
        time.sleep(interval)
        try:
            tokens = _post(
                f"https://{DOMAIN}/oauth/token",
                {
                    "client_id": CLIENT_ID,
                    "device_code": device["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            user = _decode_jwt_payload(tokens["id_token"])
            user_id = user.get("email") or user.get("sub")
            name = user.get("name", user_id)
            print(f"\n✓ 歡迎，{name}", file=sys.stderr, flush=True)
            print(user_id)  # stdout — captured by entrypoint.sh
            sys.exit(0)

        except urllib.error.HTTPError as e:
            body = json.loads(e.read())
            err = body.get("error", "")
            if err == "authorization_pending":
                continue
            elif err == "slow_down":
                interval += 5
                continue
            else:
                print(f"\n授權失敗：{body.get('error_description', err)}", file=sys.stderr)
                sys.exit(1)

    print("\n授權逾時", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
