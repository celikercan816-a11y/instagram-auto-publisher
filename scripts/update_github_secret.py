"""Writes a value into a GitHub Actions repository secret via the GitHub REST API,
using libsodium sealed-box encryption as required by that API. Never logs the
plaintext value. Requires GH_PAT (a PAT with 'Secrets: read and write' repo
permission) and GH_REPO ('owner/repo') in the environment.
"""
import base64
import os

import requests
from nacl import encoding, public


def _encrypt(public_key_b64: str, secret_value: str) -> str:
    public_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def set_github_secret(repo: str, pat: str, secret_name: str, secret_value: str) -> None:
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    key_resp = requests.get(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        headers=headers,
        timeout=30,
    )
    key_resp.raise_for_status()
    key_data = key_resp.json()

    encrypted_value = _encrypt(key_data["key"], secret_value)

    put_resp = requests.put(
        f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted_value, "key_id": key_data["key_id"]},
        timeout=30,
    )
    put_resp.raise_for_status()


def main() -> int:
    import sys

    if len(sys.argv) != 3:
        print("Kullanim: python -m scripts.update_github_secret SECRET_ADI SECRET_DEGERI")
        return 1
    secret_name, secret_value = sys.argv[1], sys.argv[2]

    repo = os.environ.get("GH_REPO", "").strip()
    pat = os.environ.get("GH_PAT", "").strip()
    if not (repo and pat):
        raise SystemExit("GH_REPO ve GH_PAT ortam degiskenleri gerekli")

    set_github_secret(repo, pat, secret_name, secret_value)
    print(f"GitHub secret guncellendi: {secret_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
