"""Read-only connectivity/permission check. Never publishes anything.

1. Confirms required .env variables are present (never prints their values).
2. GET /{IG_USER_ID}?fields=id,username,account_type,media_count
   -> proves the access token is valid and instagram_business_basic works,
      and that IG_USER_ID points at a reachable professional account.
3. GET /{IG_USER_ID}/content_publishing_limit
   -> this endpoint is part of the Content Publishing API and requires the
      instagram_business_content_publish permission, so a successful response
      proves the token has publish rights WITHOUT creating or publishing any
      media container.
"""
import requests

from src.config import Config, ConfigError
from src.instagram_api import API_VERSION, GRAPH_BASE, InstagramAPIError, InstagramClient

REQUIRED = ["IG_APP_ID", "IG_APP_SECRET", "IG_REDIRECT_URI", "IG_ACCESS_TOKEN", "IG_USER_ID"]


def check_env_presence() -> bool:
    import os
    ok = True
    for name in REQUIRED:
        present = bool(os.environ.get(name, "").strip())
        print(f"  {name}: {'VAR' if present else 'EKSIK'}")
        ok = ok and present
    return ok


def main() -> int:
    print("1) .env degiskenleri kontrol ediliyor (degerler gosterilmiyor)...")
    if not check_env_presence():
        print("\nSonuc: Bazi degiskenler eksik. Yukaridaki '.env' dosyasini tamamla.")
        return 1

    try:
        config = Config.load(require_token=True)
    except ConfigError as e:
        print(f"\nSonuc: Config hatasi: {e}")
        return 1

    client = InstagramClient(config)

    print("\n2) Hesap erisimi test ediliyor (GET /{ig-id}?fields=id,username,account_type,media_count)...")
    try:
        resp = requests.get(
            f"{GRAPH_BASE}/{config.ig_user_id}",
            params={"fields": "id,username,account_type,media_count", "access_token": config.access_token},
            timeout=30,
        )
        data = resp.json()
        if resp.status_code >= 400:
            err = data.get("error", {})
            print(f"   BASARISIZ (HTTP {resp.status_code}): {err.get('message', resp.text)}")
            print(f"   error code={err.get('code')} subcode={err.get('error_subcode')} type={err.get('type')}")
            return 2
        print(f"   OK -> username=@{data.get('username')} account_type={data.get('account_type')} "
              f"media_count={data.get('media_count')} id={data.get('id')}")
        if data.get("account_type") not in ("BUSINESS", "MEDIA_CREATOR", "CREATOR"):
            print(f"   UYARI: account_type beklenmedik ({data.get('account_type')}). "
                  "Hesabin Instagram'da Business/Creator (professional) oldugundan emin ol.")
    except requests.RequestException as e:
        print(f"   BASARISIZ (network): {e}")
        return 2

    print("\n3) Icerik yayinlama izni test ediliyor (GET /{ig-id}/content_publishing_limit, hicbir sey yayinlamaz)...")
    try:
        limit = client.get_publishing_limit()
        usage = limit.get("quota_usage")
        total = limit.get("config", {}).get("quota_total")
        print(f"   OK -> instagram_business_content_publish yetkisi calisiyor. "
              f"Son 24 saatte kullanilan: {usage}/{total}")
    except InstagramAPIError as e:
        print(f"   BASARISIZ: {e}")
        return 3

    print("\nInstagram API baglantisi hazir")
    print(f"(API version: {API_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
