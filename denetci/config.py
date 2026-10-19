"""Merkezî ayarlar, yollar ve HTTP başlıkları."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"          # sqlite db + opsiyonel oturum
OUT = ROOT / "out"            # video başına kareler / transkript / analiz
REPORTS = ROOT / "reports"    # HTML raporlar

STATE_DB = DATA / "denetci.db"
STORAGE_STATE = DATA / "storage_state.json"   # `login` ile bir kez kaydedilir

# --- YouTube istekleri (yalnızca metin sayfaları; indirme yok) -------------
POLITE_DELAY = 0.8            # istekler arası gecikme (sn)
REQUEST_TIMEOUT = 20
REQUEST_RETRIES = 2
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

# --- İzleme / kare yakalama ------------------------------------------------
FRAMES_DEFAULT = 8           # normal video başına kare (örneklem modu)
FRAMES_SHORT = 6             # Shorts başına kare
FRAMES_REALTIME_FACTOR = 2   # gerçek zamanlı modda kare çarpanı
PLAYER_TIMEOUT_MS = 20000    # oynatıcı arama üst sınırı

# --- Analiz ----------------------------------------------------------------
MAX_TRANSCRIPT_CHARS = 6000  # analize giren transkript üst sınırı
MAX_DESC_CHARS = 1200        # analize giren açıklama üst sınırı
VLM_MAX_FRAMES = 6           # VLM'e giden kare sayısı
VLM_TIMEOUT = 120            # VLM yanıtı üst sınırı (sn)

def ensure_dirs() -> None:
    for path in (DATA, OUT, REPORTS):
        path.mkdir(parents=True, exist_ok=True)
