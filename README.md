# instagram-auto-publisher

Instagram Creator hesabına, Meta'nın resmi **Instagram API with Instagram Login**
(Content Publishing API) akışını kullanarak zamanlanmış otomatik paylaşım yapan,
tamamen ücretsiz (GitHub Actions + GitHub üzerinde barındırılan medya) bir sistem.

Kullanılan API dokümantasyonu (2026-08-31 itibarıyla doğrulandı):
https://developers.facebook.com/docs/instagram-platform/content-publishing/
https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-instagram-login/business-login

Eski **Instagram Basic Display API** ve eski `instagram_content_publish` izni
(27 Ocak 2025'te kaldırıldı) kullanılmıyor. Facebook Sayfası bağlama zorunluluğu yok.

## Nasıl çalışıyor

```
content_queue.json  <-- id, media_type, media_url, caption, scheduled_at, status, ...
        |
        |  her 15 dakikada bir (GitHub Actions cron)
        v
src/publisher.py  --> zamanı gelmiş "pending" kayıtları bulur
        |
        v
src/instagram_api.py --> graph.instagram.com'a container oluşturur, publish eder
        |
        v
content_queue.json güncellenir (status=published/failed) + logs/publish_log.jsonl'a yazılır
        |
        v
GitHub Actions bu değişiklikleri repoya geri commit'ler (durum kalıcı olur)
```

Bilgisayarınız kapalı olsa bile çalışır çünkü zamanlayıcı GitHub'ın sunucularında koşar.

## Klasör yapısı

- `content_queue.json` — paylaşım kuyruğu (tek kaynak, git ile versiyonlanır)
- `media/` — repoya commit'lenen görsel/video dosyaları (Instagram, herkese açık bir
  URL istediği için `raw.githubusercontent.com` üzerinden servis edilir)
- `logs/publish_log.jsonl` — her denemenin (başarılı/başarısız) satır satır kaydı
- `src/` — API istemcisi, kuyruk yönetimi, zamanlayıcı mantığı
- `scripts/` — bir kere çalıştırılan kurulum scriptleri (OAuth, token yenileme, GitHub secret güncelleme) ve `add_to_queue.py`
- `.github/workflows/publish.yml` — her 15 dakikada bir çalışan yayın zamanlayıcı
- `.github/workflows/refresh-token.yml` — haftalık token yenileme (60 günlük token süresi dolmadan)

## Kurulum

### 1. Meta Developer App oluştur (MANUEL — sen yapmalısın)

1. https://developers.facebook.com/apps adresine git, zaten giriş yaptın.
2. **Create App** → tür olarak **"Other"** → kullanım amacı olarak **"Business"** seç.
3. Uygulama oluştuktan sonra sol menüden **Add Product** → **Instagram** kartını bul → **Set up**.
4. Açılan **"API setup with Instagram login"** sihirbazında:
   - **1. Instagram business login'i ayarla** bölümünde Instagram Creator hesabınla giriş yapıp izin ver.
   - **3. Business login settings** kısmında bir **OAuth redirect URI** ekle. Basit tutmak için `https://localhost/` yazabilirsin (sayfa gerçekten açılmasa da tarayıcı adres çubuğundaki kodu kullanacağız).
   - Aynı ekranda **Instagram App ID** ve **Instagram App Secret** değerlerini göreceksin — bunları kopyala (bana yapıştırma, .env dosyasına gireceksin — aşağıda 2. adım).
5. **App Review** gerekip gerekmediğini kontrol et: Uygulama "Development" modundeyken sadece App Dashboard'da **Roles > Instagram testers** altında eklediğin (ve Instagram tarafında daveti kabul ettiğin) hesaplar API'yi kullanabilir. Kendi hesabını test kullanıcısı olarak eklemen gerekebilir — ekranda böyle bir adım çıkarsa dur ve bana söyle, birlikte tamamlayalım. Sadece kendi hesabına paylaşım yapacaksan **Live moda geçmen / App Review'dan geçmen gerekmez.**

### 2. .env dosyasını doldur (senin bilgisayarında, lokal)

```
cd C:\Users\Mustafa\instagram-auto-publisher
copy .env.example .env
```

`.env` dosyasını aç, şunları doldur:
- `IG_APP_ID`, `IG_APP_SECRET` — yukarıdaki adımdan
- `IG_REDIRECT_URI` — App Dashboard'a kaydettiğin ile birebir aynı olmalı (örn. `https://localhost/`)

`.env` dosyası `.gitignore` içinde — asla repoya commit edilmeyecek.

### 3. Bağımlılıkları kur ve OAuth ile giriş yap

```
pip install -r requirements.txt
python -m scripts.generate_auth_url
```

Çıkan URL'yi tarayıcıda aç, Instagram hesabınla giriş yap, izinleri onayla.
Tarayıcı seni `redirect_uri`'ye yönlendirecek (sayfa "bağlanılamıyor" dese bile
sorun değil) — **adres çubuğundaki `?code=...` değerini kopyala.**

```
python -m scripts.exchange_code_for_token "KOPYALADIGIN_CODE"
```

Bu komut `IG_ACCESS_TOKEN` (60 gün geçerli) ve `IG_USER_ID` değerlerini otomatik
olarak `.env` dosyana yazar. Token'ın kendisini sana göstermem — sadece ilk/son
birkaç karakteri maskeli şekilde terminalde görürsün.

### 4. GitHub reposu (TAMAMLANDI)

`gh` CLI kuruldu (winget), tarayıcı üzerinden device-code ile giriş yapıldı
(`workflow` kapsamı dahil), `celikercan816-a11y/instagram-auto-publisher` adında
**public** repo oluşturuldu ve proje otomatik olarak push edildi:
https://github.com/celikercan816-a11y/instagram-auto-publisher

### 5. GitHub Secrets (TAMAMLANDI — IG_ACCESS_TOKEN, IG_USER_ID, IG_APP_SECRET)

`scripts/push_secrets_via_gh.py` ile `.env` içindeki değerler, ekrana hiç
yazdırılmadan, `gh secret set` komutu üzerinden doğrudan repoya yazıldı.

### 6. GitHub Personal Access Token (MANUEL — sadece otomatik token yenileme için)

Bu adım *opsiyonel*: sadece 60 günlük access token'ın **de otomatik** yenilenmesi
için gerekli (`.github/workflows/refresh-token.yml`). Atlarsan sistemin geri
kalanı normal çalışır, sadece ~60 günde bir 3. adımı elle tekrarlaman gerekir.

1. https://github.com/settings/personal-access-tokens/new
2. **Fine-grained token**, sadece bu repoyu (`instagram-auto-publisher`) seç,
   **Repository permissions → Secrets → Read and write** izni ver.
3. Token'ı oluştur, `.env` dosyandaki `GH_PAT` alanına kendin yapıştır (bana
   gösterme). Sonra bana haber ver, `python -m scripts.push_secrets_via_gh`
   ile bunu da GitHub'a secret olarak ekleyeyim.

## Kuyruğa içerik ekleme

İki yol var:

**A) Claude ile konuşarak (önerilen):** Bana "Yarın saat 19:30'da şu görseli, şu
temada paylaş" dediğinde, caption ve hashtag'leri ben üretirim, görseli/videoyu
`media/` klasörüne koyup `scripts/add_to_queue.py` ile kuyruğa eklerim ve
değişikliği senin onayınla commit/push ederim.

**B) Elle, komut satırından:**
```
python -m scripts.add_to_queue --media-type IMAGE --media media/gorsel.jpg \
    --caption "Metin... #hashtag1 #hashtag2" \
    --scheduled-at "2026-09-01T19:30:00+03:00"
```

`--media-type` seçenekleri: `IMAGE`, `VIDEO`, `REELS`, `CAROUSEL` (carousel için
`--media` parametresini 2-10 kez tekrarla). `--scheduled-at` mutlaka UTC ofsetli
ISO 8601 formatında olmalı (örn. `+03:00` = Türkiye saati).

Aynı görsel + aynı caption ikinci kez eklenmeye çalışılırsa script reddeder
(`--allow-duplicate` ile bilinçli olarak bypass edilebilir).

## Hata takibi

- Her paylaşım denemesi `logs/publish_log.jsonl` içine (`level: success/error/warning`)
  satır olarak yazılır.
- Başarısız olan kuyruk kaydının `content_queue.json` içindeki `error` alanında
  tam hata mesajı durur (`status: "failed"`).
- Bir paylaşım başarısız olursa GitHub Actions çalıştırması kırmızı (failed)
  görünür ve GitHub'ın varsayılan ayarına göre repo sahibine e-posta bildirimi
  gider — repo Settings'te bunu kapatmadıysan ekstra bir şey kurmana gerek yok.

## Limitler / notlar

- Instagram: 24 saatte en fazla 100 API paylaşımı (carousel tek paylaşım sayılır).
  Zamanlayıcı bu limiti `content_publishing_limit` endpoint'inden kontrol eder,
  dolmuşsa o an atlar ve bir sonraki 15 dakikalık çalışmada tekrar dener.
- GitHub Actions cron'u dakikası dakikasına garanti değildir, birkaç dakika
  gecikme normaldir.
- Access token 60 gün geçerli; `refresh-token.yml` haftalık otomatik yeniler
  (5-6. adımları tamamladıysan). Yenileme başarısız olursa o workflow'un
  Actions sekmesinde kırmızı göründüğünü fark edersin.
- Videolar/reels Instagram tarafında asenkron işlenir; `publisher.py` container
  hazır olana kadar (en fazla 5 dakika) bekler.

## Test

Sistemi gerçek bir paylaşımla test etmek için:
```
python -m scripts.add_to_queue --media-type IMAGE --media media/test.jpg \
    --caption "Test paylaşımı" --scheduled-at "<2-3 dakika sonrası, ISO 8601>"
git add media/test.jpg content_queue.json && git commit -m "test" && git push
```
Sonra GitHub → Actions sekmesinden `publish.yml` çalışmasını (otomatik tetiklenir
veya "Run workflow" ile elle tetikleyebilirsin) izleyip `content_queue.json` ve
Instagram profilini kontrol et.
