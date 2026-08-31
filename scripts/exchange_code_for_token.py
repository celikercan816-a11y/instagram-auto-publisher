"""Exchanges the OAuth 'code' (from the redirect URL after login) for a long-lived
Instagram access token, and writes IG_ACCESS_TOKEN + IG_USER_ID straight into .env.

The token is never printed in full anywhere -- only its length and first/last few
characters, so you can confirm something was written without it ending up in your
terminal scrollback or in a chat transcript.

Usage:
  python -m scripts.exchange_code_for_token "THE_CODE_FROM_THE_REDIRECT_URL"
"""
import sys

import requests

from scripts._env_file import set_env_var
from src.config import Config


def masked(secret: str) -> str:
    if len(secret) <= 10:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]} (len={len(secret)})"


def main() -> int:
    if len(sys.argv) != 2:
        print('Kullanim: python -m scripts.exchange_code_for_token "CODE"')
        return 1
    code = sys.argv[1].strip()
    # a pasted redirect URL sometimes carries a trailing '#_' fragment from Instagram
    code = code.split("#")[0]

    config = Config.load(require_token=False)
    if not (config.app_id and config.app_secret):
        raise SystemExit("IG_APP_ID / IG_APP_SECRET .env icinde eksik")

    # Step 1: code -> short-lived token
    resp = requests.post(
        "https://api.instagram.com/oauth/access_token",
        data={
            "client_id": config.app_id,
            "client_secret": config.app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": config.redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()["data"][0]
    short_token = payload["access_token"]
    user_id = str(payload["user_id"])
    print(f"Kisa omurlu token alindi ({masked(short_token)}), user_id={user_id}")

    # Step 2: short-lived -> long-lived (60 day) token
    resp2 = requests.get(
        "https://graph.instagram.com/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": config.app_secret,
            "access_token": short_token,
        },
        timeout=30,
    )
    resp2.raise_for_status()
    long_payload = resp2.json()
    long_token = long_payload["access_token"]
    expires_in_days = long_payload.get("expires_in", 0) // 86400
    print(f"Uzun omurlu token alindi ({masked(long_token)}), ~{expires_in_days} gun gecerli")

    set_env_var("IG_ACCESS_TOKEN", long_token)
    set_env_var("IG_USER_ID", user_id)
    print()
    print(".env dosyasina yazildi: IG_ACCESS_TOKEN, IG_USER_ID")
    print("Simdi bu iki degeri GitHub repo Secrets'a da eklemen gerekiyor (README adim 6).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
