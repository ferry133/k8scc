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
    print("授權完成後請關閉該視窗，回到此頁繼續操作", file=sys.stderr, flush=True)
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
            # Use /userinfo endpoint — works regardless of id_token presence
            access_token = tokens.get("access_token")
            if not access_token:
                print(f"\n無法取得 access_token，回應：{list(tokens.keys())}", file=sys.stderr)
                sys.exit(1)
            ui_req = urllib.request.Request(
                f"https://{DOMAIN}/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urllib.request.urlopen(ui_req) as r:
                user = json.loads(r.read())
            user_id = user.get("email") or user.get("sub", "unknown")
            name = user.get("name", user_id)
            print(f"\n✓ 歡迎，{name}", file=sys.stderr, flush=True)
            print(user_id)  # stdout — captured by claude-session
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
        except Exception as e:
            print(f"\n[錯誤] {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            sys.exit(1)

    print("\n授權逾時", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
