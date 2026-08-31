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

## Otonom içerik katmanı

`publish.yml` ve `src/publisher.py` yukarıdaki gibi değişmeden çalışmaya devam
ediyor. Üstüne, kuyruğu kendi kendine dolduran bir katman eklendi:

```
her Pazar (weekly-plan.yml)
  -> src/content_planner.generate_weekly_plan()
  -> weekly_content_plan.json (6 slot, hepsi IMAGE post -- reels şimdilik devre dışı, gün/saat/tema/durum)

her gün (daily-content-fill.yml)
  -> src/content_planner.ensure_queue_filled()
     -> onumuzdeki 7 gunde < 3 hazir icerik varsa:
        -> src/image_generator: once media/library/<tema>/, yoksa Hugging Face (ucretsiz) ile uret
        -> src/content_bank: caption + hashtag (yerel sablon+rotasyon, LLM cagrisi yok)
        -> src/content_quality.run_quality_control(): 0-100 puan
           >=70 -> status=pending (publish.yml normal sekilde alir)
           <70  -> status=needs_review (otomatik yayinlanmaz)
        -> ucretsiz kota tukenirse (HTTP 402): bu calistirmayi durdur, kalan
           slotlari "planned" birak (yarin tekrar denenir), ucretli servise GECME
  -> src/performance.update_history_with_performance() (Instagram Insights, best-effort)
  -> content_queue.json + content_history.json + weekly_content_plan.json commit edilir
```

**Reels şimdilik devre dışı** (kullanıcı talebiyle): `SLOT_TEMPLATE`'teki tüm
slotlar `"post"` (IMAGE). Video üretim servisi bağlanana kadar reels
üretilmiyor; ne slideshow+müzik gibi bir yöntem otomatik devreye giriyor ne de
boş bir "needs_generation" kaydı oluşuyor -- slot tipi zaten hiç "reels"
üretmiyor. Reels'i geri açmak istersen `src/content_planner.py`'deki
`SLOT_TEMPLATE`'e `"reels"` girdileri eklemek ve bir üretim yöntemi (video API
veya onayladığın başka bir yöntem) bağlamak yeterli -- REELS `media_type`
desteği `src/image_generator.py` ve `src/instagram_api.py`'de zaten duruyor.

- `content_history.json` — yayınlanan her içeriğin tema/caption özeti/hashtag
  seti/görsel fingerprint/insights kaydı. Tekrar kontrolü ve performans
  öğrenmesi buradan besleniyor.
- `strategy_weights.json` — `scripts/update_performance.py` en az 5 örnek
  biriken tema/saat/içerik-türü gruplarının ortalama performansını buraya
  yazar. `src/content_bank._effective_weights()` bunu okuyup temaların
  ağırlığını **her yönde en fazla ±%20 (temanın kendi tabanına göre)**
  kaydırır ve yeniden normalize eder -- iyi performans gösteren tema biraz
  daha sık, kötü gösteren biraz daha az seçilir ama hiçbir tema tek başına
  hesaba hakim olamaz (2026-08-31'de kullanıcı talebiyle aktifleştirildi).
- Caption/hashtag üretimi **yerel bir şablon+rotasyon bankası**
  (`src/content_bank.py`), bir LLM API çağrısı değil -- headless GitHub
  Actions job'ının ek bir ücretli anahtara bağımlı olmaması için bilinçli bir
  tercih. `content_quality.py` aynı/çok benzer caption ve hashtag setinin
  tekrarını `content_history.json`'a bakarak engelliyor.
- Reels şimdilik tamamen devre dışı (bkz. "Reels / video üretimi" bölümü) --
  telif riskli slideshow+müzik gibi bir yöntem otomatik devreye girmiyor.

## İçerik stratejisi (2026-08-31)

Hedef: rastgele AI görseli paylaşan bir hesap değil, tutarlı/premium/gerçek
hissi veren bir lifestyle hesabı. 5 tema, haftalık *ortalama* hedef dağılım
(`src/content_bank.THEME_WEIGHTS`, performans verisiyle hafifçe nudge'lanır):

| Tema | Hedef ağırlık | İçerik |
|---|---|---|
| `travel_landscape` | %35 | seyahat / şehir / manzara |
| `style_fashion` | %25 | erkek stil / kombin / saat / aksesuar |
| `lifestyle` | %20 | kahve / mekan / günlük yaşam |
| `automotive` | %10 | otomobil / yolculuk atmosferi |
| `creative_concept` | %10 | özel yaratıcı konsept kareler |

Bu bir *ağırlık*, garanti değil -- `pick_theme_for_slot` aynı temayı art arda
seçmiyor, bu yüzden tek bir haftanın dağılımı hedeften sapabilir (örn. bir
hafta hiç `automotive` çıkmayabilir); zaman içindeki ortalama hedefe yakınsar.

Diğer kurallar (`src/content_bank.py` + `src/content_quality.py`'de uygulanıyor):
- Caption uzunluğu değişken: bankada hem tek cümlelik hem 2-3 cümlelik
  girdiler var, rastgele seçiliyor.
- Hashtag sayısı her gönderide **4-8 arası rastgele** (sabit değil), `#viral`
  `#fyp` gibi engagement-bait etiketler hiç havuzda yok, ayrıca
  `content_quality.check_hashtags` bunları görürse reddediyor.
- Her image prompt'a ortak bir "gerçekçilik" son eki ekleniyor (doğal
  doku/tane, kamera-gerçekçi renk, CGI/render/sürreal görünümden kaçınma, "AI
  klişesi" aşırı kusursuzluktan kaçınma) -- `src/content_bank._REALISM_SUFFIX`.
- Hesap sahibinin kendisini taklit eden bir AI görseli **hiçbir zaman**
  üretilmiyor: sistem hiçbir prompt'a "bu hesabın sahibi" gibi bir referans
  vermiyor ve kullanıcının gerçek fotoğrafını image-to-image/yüz-tutarlılığı
  için kullanan hiçbir kod yolu yok. Kullanıcının kendi gerçek fotoğrafları
  sadece `media/library/<tema>/`'ye elle konursa (aynı `ercan-test-post.jpeg`
  gibi) kullanılıyor -- bu, sistemin `find_local_media` önceliği sayesinde
  AI üretiminden önce otomatik tercih ediliyor.

## Klasör yapısı

- `content_queue.json` — paylaşım kuyruğu (tek kaynak, git ile versiyonlanır)
- `content_history.json` — yayınlanan içeriklerin geçmişi (tekrar kontrolü + performans)
- `weekly_content_plan.json` — haftalık içerik planı (gün/saat/tema/durum)
- `strategy_weights.json` — performansa göre tema/saat/tür ortalamaları (raporlama)
- `media/` — repoya commit'lenen görsel/video dosyaları (Instagram, herkese açık bir
  URL istediği için `raw.githubusercontent.com` üzerinden servis edilir)
  - `media/library/<tema>/` — buraya gerçek/kendi fotoğraflarınızı koyarsanız
    AI görsel üretiminden önce öncelikle bunlar kullanılır
  - `media/generated/` — AI ile üretilen görseller (dosya adı = queue item id)
- `logs/publish_log.jsonl`, `logs/image_generation_log.jsonl` — satır satır kayıtlar
- `src/` — API istemcisi, kuyruk yönetimi, içerik bankası, görsel üretimi,
  kalite kontrolü, planlayıcı, performans analizi
- `scripts/` — bir kere çalıştırılan kurulum scriptleri (OAuth, token yenileme,
  GitHub secret güncelleme), `add_to_queue.py` ve otonom pipeline'ın günlük/haftalık girişleri
- `.github/workflows/publish.yml` — her 15 dakikada bir çalışan yayın zamanlayıcı (değişmedi)
- `.github/workflows/refresh-token.yml` — haftalık token yenileme (değişmedi)
- `.github/workflows/weekly-plan.yml` — her Pazar haftalık planı üretir
- `.github/workflows/daily-content-fill.yml` — her gün kuyruğu doldurur + performans çeker

## AI görsel üretimi

Servis: **Hugging Face Inference Providers, model `black-forest-labs/FLUX.1-schnell`**
(2026-08-31 itibarıyla güncel dokümantasyon doğrulandı:
https://huggingface.co/docs/inference-providers/en/index ,
https://huggingface.co/docs/inference-providers/en/pricing). **Ücretsiz, kredi
kartı gerektirmiyor**: her ücretsiz HF hesabı ayda ~$0.10 Inference Providers
kredisi alıyor, kart bilgisi istenmiyor. Kota bitince istek HTTP 402 ile
başarısız oluyor (otomatik ücretlendirme yok) -- sistem bunu yakalayıp o
slotu sessizce `needs_generation` yapıyor, **hiçbir koşulda ücretli bir
servise otomatik geçmiyor** (`src/image_generator.py`, `QuotaExhaustedError`).

OpenAI entegrasyonu koda opsiyonel olarak bırakıldı (`IMAGE_PROVIDER=openai`
+ `OPENAI_API_KEY` ile elle açılabilir) ama **varsayılan ve aktif olan
sağlayıcı değil** -- hiçbir otomatik çağrı OpenAI'a gitmiyor.

Üretim önceliği (`src/image_generator.get_media_for_theme`): önce
`media/library/<tema>/`'daki kullanılmamış gerçek fotoğraf, yoksa AI üretimi.
Rastgele internet görseli hiçbir koşulda kullanılmıyor.

Her üretilen görsel `content_quality.check_image` ile kontrol ediliyor (bozuk
dosya, düşük çözünürlük, yanlış en-boy oranı); başarısız olursa max 3 kez
yeniden denenir. **Not:** OpenAI kaldırıldığı için şu an bozuk el/yüz/nesne
gibi anatomik hataları otomatik tespit eden ayrı bir vision-QC adımı yok --
sadece yapısal kontrol (çözünürlük/oran/bozuk dosya) otomatik. Bu bilinen bir
sınırlama; yeni bir prompt şablonunu ilk kez kullanmadan önce üretilen
görseli bir kez elle gözden geçirmek hâlâ faydalı.

**Gerekli manuel adım:** `HF_TOKEN` -- https://huggingface.co/settings/tokens/new
üzerinden **"Make calls to Inference Providers"** izniyle bir fine-grained
token oluştur (kart bilgisi istenmez). Oluşturunca `.env` dosyana kendin
ekle, haber ver -- `scripts/push_secrets_via_gh.py` ile GitHub Secrets'a
ekleyeyim.

## Reels / video üretimi

**Şimdilik tamamen devre dışı** (kullanıcı talebiyle, 2026-08-31). Sistem
sadece ücretsiz üretilebilen IMAGE feed gönderileri oluşturuyor;
`weekly_content_plan.json`'da hiç `"reels"` slotu yok. Video üretim servisine
karar verilirse seçenekler:
- **Kısa video üretim API'si** (ör. Runway, Kling, Pika, Luma) — gerçek AI
  video, ücretli ve ayrı bir API key gerektirir.
- **Görsel + statik slayt** (AI görsellerden basit bir slideshow, müziksiz,
  sadece görsel geçişleri) — düşük maliyetli ama "Reels" formatının asıl
  gücü olan hareketli video hissini vermez.
- Müzik eklenen slideshow'lar **otomatik devreye alınmayacak** çünkü telif
  riski taşıyor (Instagram'ın kendi telifsiz müzik kütüphanesi API ile
  otomatik seçilemiyor).

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
