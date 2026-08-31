"""Refreshes the long-lived Instagram access token and pushes the new value
straight into the GitHub Actions secret IG_ACCESS_TOKEN. Meant to run on a
schedule (see .github/workflows/refresh-token.yml) well before the 60-day
expiry -- Meta allows refreshing anytime after the token is 24h old.
"""
import os

import requests

from scripts.update_github_secret import set_github_secret


def main() -> int:
    access_token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    app_secret = os.environ.get("IG_APP_SECRET", "").strip()
    repo = os.environ.get("GH_REPO", "").strip()
    pat = os.environ.get("GH_PAT", "").strip()

    missing = [n for n, v in [
        ("IG_ACCESS_TOKEN", access_token), ("IG_APP_SECRET", app_secret),
        ("GH_REPO", repo), ("GH_PAT", pat),
    ] if not v]
    if missing:
        raise SystemExit(f"Eksik ortam degiskenleri: {', '.join(missing)}")

    resp = requests.get(
        "https://graph.instagram.com/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": access_token},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise SystemExit(f"Token yenileme basarisiz (HTTP {resp.status_code}): {resp.text}")

    new_token = resp.json()["access_token"]
    set_github_secret(repo, pat, "IG_ACCESS_TOKEN", new_token)
    print("IG_ACCESS_TOKEN basariyla yenilendi ve GitHub secret'a yazildi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
