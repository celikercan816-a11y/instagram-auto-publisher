"""Prints the Instagram Business Login authorization URL to open in a browser.
Run once, manually, as part of initial setup (see README step 3)."""
from urllib.parse import urlencode

from src.config import Config

SCOPES = "instagram_business_basic,instagram_business_content_publish"


def main() -> None:
    config = Config.load(require_token=False)
    if not config.app_id:
        raise SystemExit("IG_APP_ID is not set in .env")

    params = {
        "client_id": config.app_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
    }
    url = "https://www.instagram.com/oauth/authorize?" + urlencode(params)
    print("Bu URL'yi tarayicida ac, Instagram hesabinla giris yap ve izinleri onayla:")
    print()
    print(url)
    print()
    print("Onayladiktan sonra tarayici seni redirect_uri'ye yonlendirecek (sayfa acilmasa bile).")
    print("Adres cubugundaki '?code=...' degerini kopyala ve su komutu calistir:")
    print('  python -m scripts.exchange_code_for_token "YAPISTIRILAN_CODE"')


if __name__ == "__main__":
    main()
