# video-denetci

YouTube videolarının **içerik doğrululuğunu** denetleyen bot:
başlık içeriği yansıtıyor mu, clickbait var mı, içerik gerçek mi
(AI üretilmiş görsel / stok tekrarı / uydurma sinyalleri) — tüm bunlar
**video dosyası indirilmeden**, gerçek bir tarayıcıda oynatılarak yapılır.

> **Amaç:** içerik denetimi (sahte/clickbait tespiti). Araç izlenme sayısı,
> izlenme süresi ya da herhangi bir metrik üretmez/şişirmez.

**Anahtar kelimeler:** YouTube sahte video tespiti · clickbait bulucu ·
YouTube içerik doğrulama aracı · Shorts analiz botu · AI ile sahte içerik
tespiti · başlık–içerik uyum denetimi · YouTube denetleme botu · fake video
detection · clickbait detector · YouTube content authenticity check

## Nasıl çalışıyor?

```
scan     → yt-dlp flat mod: /videos + /shorts sekmeleri, devam sayfaları dahil
           (yalnız metadata; indirme yok)                → data/denetci.db
watch    → Playwright gerçek Chromium: video oynatılır, oynatıcıdan kare
           alınır (örneklem modu seek ile hızlı; --realtime gerçek izleme)
           opsiyonel hesap oturumu: `login` (elle giriş, bir kez)
analyze  → 1) anahtarsız sezgisel: başlık↔transkript uyumu, sayısal iddia
              kanıtı, clickbait kalıpları
           2) opsiyonel VLM: kareler + metin → gerçeklik skoru + sinyaller
report   → reports/denetim_*.html (+ reports/latest.html)
```

Transkript/altyazı, YouTube'un altyazı akışından çekilir (yt-dlp json3/vtt;
`api/timedtext` ham istekleri BotGuard `pot` token'ı gerektirdiği için boş
döner — yt-dlp bu isteği kendi innerTube akışı üzerinden çözer). Altyazı dili
**başlık diline göre otomatik seçilir** (İngilizce başlıkta İngilizce
transkript) ki uyum aynı dilde ölçülsün. Videonun kendisi hiçbir adımda diske
inmez — yalnızca oynatıcıdan anlık kare (JPEG) yakalanır.

## Kurulum

```bash
pip install -r requirements.txt
playwright install chromium      # bu makinede kurulu
python -m denetci --help
```

## Kullanım

```bash
# 1) Kanalı tara — VİDEOLAR + SHORTS sekmeleri, limit yoksa tamamı
python -m denetci scan @somechannel

# 2) İzle (kare yakala) — varsayılan hızlı örneklem modu
python -m denetci watch
python -m denetci watch --realtime      # video baştan sona oynar (yavaş)

# 3) Denetle — anahtarsız sezgisel katman
python -m denetci analyze
python -m denetci analyze --retry      # sonradan yeniden denetme (transkript de tazelenir)

# 4) Rapor
python -m denetci report

# 0) İsterseniz kanal arayın — ad/URL alın, scan hedefine yapıştırın
python -m denetci search "mkbhd"

# Hepsi tek komut:
python -m denetci run @somechannel --limit 20

# Yerel panel (GUI): kanal arama, tarama, izleme, denetim, rapor — tarayıcıda
python -m denetci panel

# Gerçeklik (görsel) skoru için — OpenAI uyumlu herhangi bir uç nokta:
export VLM_BASE_URL=https://api.openai.com/v1
export VLM_API_KEY=sk-...
export VLM_MODEL=gpt-4o-mini
python -m denetci run @somechannel
# ya da tek seferlik:
python -m denetci run @somechannel --vlm-base https://api.openai.com/v1 --vlm-key sk-...
```

### Panel (GUI)

```bash
python -m denetci panel                 # http://127.0.0.1:8788 (tarayıcıda açılır)
python -m denetci panel --port 8789     # farklı port
python -m denetci panel --no-browser    # otomatik açma
```

Tek sayfalık yerel arayüz (hiçbir sunucuya veri gitmez, yalnızca 127.0.0.1):

- **Kanal arama çubuğu** — YouTube'da kanal aranır; sonuçdan tıklayınca hedef
  otomatik dolar (CLI karşılığı: `denetci search "mkbhd"`).
- Tarama / **İzleme** / Denetim / Rapor butonları + "Hepsini çalıştır".
- **izlemeyi pencerede göster**: işaretle Chromium penceresi açılır ve botun
  videoyu oynattığını canlı görürsünüz (varsayılan: gizli pencere).
- Canlı ilerleme günlüğü, durum çipleri (listeli/izlendi/denetlendi/hata),
  video tablosu: kare thumbnail'ı, tip (Shorts/video), skorlar, karar rozeti.
- VLM ayarları formda girilir ve `data/panel_settings.json`'a kaydolur
  (anahtar istemci tarafında saklanmaz).

### Ücretsiz / yerel VLM seçenekleri

| Uç nokta | `--vlm-base` | `--vlm-model` |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Ollama (yerel) | `http://localhost:11434/v1` | `qwen2.5vl:7b` (görsel destekli) |
| LM Studio | `http://localhost:1234/v1` | yüklü model |
| OpenRouter / Groq | ilgili `.../v1` adresi | uygun VLM adı |

VLM yapılandırılmazsa analiz yine çalışır; **gerçeklik skoru** yerine
"-" gösterilir, başlık↔içerik uyumu + clickbait + iddia kanıtı (anahtarsız)
her zaman üretilir.

### Hesap oturumu (opsiyonel)

Yaş kısıtlı/abone içerik için oturum kaydedilebilir:

```bash
python -m denetci login    # açılan pencerede giriş yapın, Enter'a basın
```

`data/storage_state.json` sonraki `watch` otomatik kullanır. Analiz için
zorunlu değildir.

## İzleme belgesi (kanıtlanabilir "izlendi")

İddia/denetim süreçlerinde "bu video izlendi" sözü **belgeyle** desteklenir.
Her başarılı izlemede `out/<id>/izleme_belgesi.json` yazılır:

- **zaman damgası** (UTC), oynatılan süre / video süresi, mod, çözünürlük
- her karenin **SHA-256 özeti** + boyut + yakalama anı
- transkript önbelleğinin SHA-256 özeti
- belgenin **içerik özeti** (kanonik JSON'un SHA-256'sı)

Üçüncü taraf doğrulama (belge ya da kareler değiştirilirse özet uyuşmaz):

```bash
python -m denetci verify              # tüm kayıtlar
python -m denetci verify --video ID   # tek video
```

Raporun her kartında belge özeti+zamanı, panelde 🔎 rozeti, panelde ayrıca
**"Belge doğrula"** butonu vardır. Belge, aracın kendi yerel kaydıdır;
YouTube'da izlenme/etkileşim metriği üretmez/şişirmez.

## Çıktılar

```
data/denetci.db            ← durum (listed → watched → analyzed)
out/<video_id>/
  kareler/kare_001.jpg ... ← oynatıcıdan yakalanmış kareler
  transkript.txt           ← altyazı/metin
  aciklama.txt
  analiz.json              ← skorlar, sinyaller, karar
reports/latest.html        ← denetim raporu (sonuç kartları + thumbnail)
```

### Skorlar nasıl okunur?

- **başlık↔içerik**: başlığın anlamsal kelimelerinin transkriptte geçme oranı
  (VLM varsa VLM judgement'i) — 100 = başlık birebir içeriği yansıtıyor.
- **iddia kanıtı**: başlıktaki sayılar ("3 yol", "%80") içerikte geçiyor mu ✓/✗.
- **gerçeklik**: yalnızca VLM katmanından gelir — karelerde AI üretilmiş yüz/
  metin bozulması, stok tekrarı, altyazı-görsel çelişkisi gibi sinyaller.
- **karar**: genel ≥ 70 güvenilir · 45–69 şüpheli · < 45 güvensiz/uyumsuz.

## Lisans ve ücretli kullanım

Çift lisans — ayrıntı: [LICENSE.md](LICENSE.md)

| Kullanım | Ücret | Koşul |
|---|---|---|
| Bireysel · eğitim · araştırma · gazetecilik · STK | **Ücretsiz** | yalnız içerik denetimi + atıf |
| Şirket / ajans / SaaS / müşteriye hizmet / gömülü ürün | **Ücretli** | önceden yazılı ticari lisans (Issue/DM) |

Ücretsiz katmanda video limiti yoktur; ücretli lisans, kullanım kapsamı +
destek + varsa işlem limiti yazılı sözleşmede belirlenir. Araç hiçbir koşulda
izlenme/etkileşim metriği üretmez — bu, lisansın da ihlal sebebidir.

## Sınırlar

- Altyazı **kapalı** videolarda transkript olmaz; uyum skoru açıklama ile
  ölçülür, gerçeklik yalnız VLM kareleriyle değerlendirilir.
- **Anlatım olmayan** kısa videolarda (müzik/aksiyon Shorts: transkript
  < 25 içerik kelimesi) uyum "ölçülemez" → karar *değerlendirilmedi* verir;
  burada anlamlı yargı VLM'in kare incelemesiyle gelir.
- Video kaldırılmış/gizli/yaş kısıtlı ise `watch` hata olarak işaretler
  (`--retry` ile yeniden dene).
- İstekler kibar gecikmelidir (`config.POLITE_DELAY`); YouTube hizmet
  şartlarına uyumdan kullanıcı sorumludur. Araç yalnızca **içerik denetimi**
  için vardır; izlenme/etkileşim metriği üretmez.
