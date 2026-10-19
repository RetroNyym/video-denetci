"""Başlık–içerik uyumu ve gerçeklik denetimi.

İki katman:

1. **Anahtarsız sezgisel** — başlık kelimelerinin transkript/açıklamada geçme
   oranı, başlıktaki sayısal iddiaların kanıtlanıp kanıtlanmadığı, clickbait
   kalıpları. Hızlı ve anahtar gerektirmez.
2. **VLM (opsiyonel)** — OpenAI uyumlu herhangi bir görsel-dil uç noktası
   (OpenAI, OpenRouter, Groq, Ollama, LM Studio ...): oynatıcıdan yakalanmış
   kareler + metin ile AI üretilmiş görsel / stok tekrarı / uydurma içerik
   sinyalleri. `--vlm-base` veya `VLM_BASE_URL` ile açılır.

Sonuçlar tek JSON'da (`out/<id>/analiz.json`) toplanır.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import requests

from . import config

# Türkçe + İngilizce anlamsız/yardımcı kelimeler
STOPWORDS = {
    "bir", "bu", "şu", "ve", "ile", "için", "gibi", "ama", "fakat", "ki",
    "mi", "mı", "mu", "mü", "ne", "da", "de", "en", "çok", "daha", "az",
    "the", "and", "for", "with", "you", "your", "that", "this", "are",
    "was", "were", "not", "can", "will", "from", "have", "has",
}

# Clickbait / abartı kalıpları (başlıkta geçme kontrolü; ek tolerance'lı eşleşme)
CLICKBAIT_WORDS = [
    "inanılmaz", "inanamayacaksınız", "şok", "şoke", "dehşet", "herkes",
    "bilmiyor", "bilmiyorsunuz", "bilmelisiniz", "kesinlikle", "kanıtlandı",
    "kanıt", "%100", "asla", "imkansız", "tıklamadan", "ömründe",
    "her şey değişecek", "gizli", "yasak", "çarpıcı", "dudağınızı uçuklatan",
    "rekor", "ilk kez", "şoke edici", "delil", "ifşa",
    "shocking", "you won't believe", "secret", "exposed", "proof",
    "never do this", "everyone is wrong", "100%",
]


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[0-9A-Za-zçğıöşüÇĞİÖŞÜ]{3,}", (text or "").lower())
    return [w for w in words if w not in STOPWORDS]


# ASCII yazım farkı: "Inanilmaz" ile "İnanılmaz" aynı ele alınır
_FOLD = str.maketrans({"ı": "i", "ş": "s", "ğ": "g", "ç": "c", "ö": "o", "ü": "u"})


def _fold(text: str) -> str:
    return (text or "").lower().translate(_FOLD)


def _has_word(haystack_folded: str, word_folded: str) -> bool:
    """Kelime başı + ek toleranslı arama.

    - "sıraları" içinde "asla" YAKALANMAZ (kelime ortası, lookbehind)
    - "herkesin" içinde "herkes" yakalanır (ek, 8 karaktere kadar)
    - "Inanilmaz" == "inanılmaz" (fold)
    """
    if not word_folded:
        return False
    pattern = (
        rf"(?<![a-z0-9]){re.escape(word_folded)}[a-z0-9]{{0,8}}(?![a-z0-9])"
    )
    return bool(re.search(pattern, haystack_folded))


# Başlıktaki "3" iddiasının içerikte "üç" olarak geçmesi için normalizasyon
_NUM_WORDS = {
    "iki": "2", "üç": "3", "uc": "3", "dört": "4", "dort": "4",
    "beş": "5", "bes": "5", "altı": "6", "alti": "6",
    "yedi": "7", "sekiz": "8", "dokuz": "9", "on": "10",
    "yirmi": "20", "otuz": "30", "kırk": "40", "kirk": "40", "elli": "50",
}


def _normalize_numbers(text: str) -> str:
    def _sub(match: re.Match) -> str:
        word = match.group(0)
        return _NUM_WORDS.get(word, word)

    return re.sub(r"[0-9a-zçğıöşü]+", _sub, (text or "").lower())


def _clamp(value) -> int | None:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def detect_lang(title: str, description: str = "") -> str | None:
    """Başlık+açıklama dilinden altyazı tercihi: 'tr' | 'en' | None (belirsiz).

    Amaç: başlık↔içerik uyumunu AYNI dilde karşılaştırmak. İngilizce başlıkta
    Türkçe çeviri altyazı seçilirse uyum yapay olarak 0 çıkar.
    """
    text = f" {(title or '').lower()} {(description or '').lower()} "
    tr_chars = sum(text.count(c) for c in "ıığşçöü")
    tr_hits = sum(
        1
        for w in (" ve ", " bir ", " için ", " nasıl ", " neden ", " ile ",
                  " bu ", " çok ", " sen ", " ben ", " siz ", " misiniz ")
        if w in text
    )
    en_hits = sum(
        1
        for w in (" the ", " and ", " for ", " with ", " you ", " to ",
                  " of ", " in ", " is ", " how ", " why ", " this ")
        if w in text
    )
    if tr_chars >= 2 or tr_hits >= 2:
        return "tr"
    if tr_chars or tr_hits:
        return None          # karışık/belirsiz → kullanıcı tercihi (--lang)
    if en_hits:
        return "en"
    return None


# --------------------------------------------------------------------------
# 1. Anahtarsız sezgisel katman
# --------------------------------------------------------------------------

# Güvenilir uyum ölçümü için gereken minimum içerik kelimesi
# (transkript+çok kısa açıklamalarda ölçüm yanıltıcı olur: örn. 20 saniyelik
#  müzik/aksiyon Shorts'ında hiç anlatım yokken başlık "0 uyum" görünür)
MIN_CORPUS_TOKENS = 25


def heuristic(item: dict, transcript: str, description: str) -> dict:
    title = item.get("title", "") or ""
    title_tokens = set(_tokens(title))
    tr_token_list = _tokens(transcript)
    desc_token_list = _tokens(description)
    tr_tokens = set(tr_token_list)
    desc_tokens = set(desc_token_list)

    uyum: int | None = None
    note = ""
    matched = 0
    if not title_tokens:
        note = "başlık boş"
    elif len(tr_token_list) >= MIN_CORPUS_TOKENS:
        matched = len(title_tokens & tr_tokens)
        uyum = round(100 * matched / len(title_tokens))
        note = f"başlık kelimelerinin %{uyum}'u transkripte geçiyor"
    elif len(desc_token_list) >= MIN_CORPUS_TOKENS:
        matched = len(title_tokens & desc_tokens)
        uyum = round(100 * matched / len(title_tokens))
        note = "transkript yok/çok kısa — karşılaştırma açıklama ile yapıldı"
    else:
        note = (
            f"veri yetersiz: transkript {len(tr_token_list)} kelime, "
            f"açıklama {len(desc_token_list)} kelime — ölçüm güvenilir değil"
        )

    # başlıktaki sayısal iddialar içerikte geçiyor mu?
    claims = re.findall(r"\d+(?:[.,]\d+)?", title)
    corpus = _normalize_numbers(f"{transcript} {description}")
    iddialar = [
        {"iddia": c, "icerikte": bool(re.search(rf"(?<!\d){re.escape(c)}(?!\d)", corpus))}
        for c in dict.fromkeys(claims)
    ]

    title_lower = _fold(title)
    cb_hits = [w for w in CLICKBAIT_WORDS if _has_word(title_lower, _fold(w))]

    return {
        "uyum_skor": uyum,
        "uyum_not": note,
        "baslik_kelime": len(title_tokens),
        "eslesen_kelime": matched,
        "iddialar": iddialar,
        "iddia_kanit_orani": (
            round(100 * sum(1 for i in iddialar if i["icerikte"]) / len(iddialar))
            if iddialar
            else None
        ),
        "clickbait": {"var": bool(cb_hits), "bulunan": cb_hits},
        "transkript_var": bool(transcript.strip()),
        "transkript_kelime": len(_tokens(transcript)),
    }


# --------------------------------------------------------------------------
# 2. VLM katmanı (OpenAI uyumlu görsel-dil modeli)
# --------------------------------------------------------------------------

_SYSTEM = (
    "Sen bir YouTube içerik denetçisisin. Görevin, başlığın video içeriğini "
    "ne kadar yansıttığını ve içeriğin gerçek/güvenilir olup olmadığını "
    "değerlendirmek. SADECE verilen karelerden ve metinden desteklenebilir "
    "yargı ver. Emin olmadığında skoru 40-60 aralığında tut. Yanıtlarını "
    "Türkçe ver ve yalnızca istenen JSON nesnesini döndür."
)

_USER_TEMPLATE = """Denetlenecek video:
- Başlık: {title}
- Tip: {kind} | Süre: {duration} sn
- Açıklama: {description}

Transkript:
{transcript}

Ekte video oynatıcısından yakalanmış {frame_count} kare var.

YALNIZCA geçerli bir JSON nesnesi döndür (Markdown bloğu, açıklama yok):
{{
  "baslik_icerik_uyumu": 0-100,
  "uyum_aciklama": "kısa gerekçe",
  "clickbait": {{"var": true, "gerekce": "kısa gerekçe"}},
  "gerceklik": {{"skor": 0-100, "sinyaller": ["örn: AI üretilmiş yüz bozulması", "stok görüntü tekrarı", "altyazı ile görsel çelişki"], "gerekce": ["kanıt 1", "kanıt 2"]}},
  "ozet": "2-3 cümlelik denetim özeti"
}}

Ölçütler: 100 = başlık içeriği birebir yansıtıyor ve içerik gerçek görünüyor;
0 = başlık içeriğe uymuyor ve/veya içerik sahte, AI üretilmiş ya da uydurma.
Türkçe alan adları ve Türkçe metin kullan.
"""


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def vlm_analyze(
    cfg: dict,
    item: dict,
    transcript: str,
    description: str,
    frame_paths: list[str],
    lang: str = "tr",
) -> tuple[dict | None, str]:
    """VLM çağrısı. Döner: (geçerli dict, "") ya da (None, hata açıklaması)."""
    duration = item.get("duration") or 0
    prompt = _USER_TEMPLATE.format(
        title=item.get("title", "") or "(başlık yok)",
        kind="Shorts" if item.get("is_short") else "normal video",
        duration=int(duration) if duration else "?",
        description=(description or "(yok)")[: config.MAX_DESC_CHARS],
        transcript=(transcript or "(transkript yok)")[: config.MAX_TRANSCRIPT_CHARS],
        frame_count=min(len(frame_paths), cfg.get("max_frames", config.VLM_MAX_FRAMES)),
    )

    content: list[dict] = [{"type": "text", "text": prompt}]
    for path in frame_paths[: cfg.get("max_frames", config.VLM_MAX_FRAMES)]:
        p = Path(path)
        if not p.exists():
            continue
        try:
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        except OSError:
            continue
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
        )

    payload = {
        "model": cfg.get("model") or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
        "max_tokens": 900,
    }
    headers = {"Content-Type": "application/json"}
    if cfg.get("key"):
        headers["Authorization"] = f"Bearer {cfg['key']}"

    url = cfg["base"].rstrip("/") + "/chat/completions"
    try:
        resp = requests.post(
            url, json=payload, headers=headers, timeout=config.VLM_TIMEOUT
        )
        resp.raise_for_status()
        body = resp.json()
        text = body["choices"][0]["message"]["content"]
    except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
        return None, f"VLM hatası: {exc}"

    data = _parse_json(text)
    if data is None:
        return None, "VLM yanıtı JSON olarak ayrıştırılamadı"

    # alan doğrulama
    data["baslik_icerik_uyumu"] = _clamp(data.get("baslik_icerik_uyumu")) or 0
    gr = data.get("gerceklik") or {}
    if not isinstance(gr, dict):
        gr = {}
    gr["skor"] = _clamp(gr.get("skor"))
    gr.setdefault("sinyaller", [])
    gr.setdefault("gerekce", [])
    data["gerceklik"] = gr
    cb = data.get("clickbait") or {}
    if not isinstance(cb, dict):
        cb = {"var": bool(cb)}
    data["clickbait"] = cb
    data.setdefault("ozet", "")
    data.setdefault("uyum_aciklama", "")
    return data, ""


# --------------------------------------------------------------------------
# Birleştirme + karar
# --------------------------------------------------------------------------

def _decision(uyum: int | None, gercek: int | None) -> tuple[str, int | None]:
    parts = [p for p in (uyum, gercek) if p is not None]
    if not parts:
        return "değerlendirilmedi", None
    genel = round(sum(parts) / len(parts))
    if gercek is None:
        if genel >= 70:
            return "uyumlu (görsel doğrulama yok)", genel
        if genel >= 45:
            return "şüpheli (görsel doğrulama yok)", genel
        return "uyumsuz — başlık içeriği yansıtmıyor", genel
    if genel >= 70:
        return "güvenilir", genel
    if genel >= 45:
        return "şüpheli", genel
    return "güvensiz — sahte/uydurma sinyali", genel


def analyze(
    item: dict,
    transcript: str,
    description: str,
    frame_paths: list[str],
    vlm_cfg: dict | None = None,
    lang: str = "tr",
) -> dict:
    """Tüm denetimi tek JSON olarak üretir."""
    heu = heuristic(item, transcript, description)

    vlm, vlm_error = None, "VLM yapılandırılmadı (--vlm-base / VLM_BASE_URL)"
    if vlm_cfg:
        vlm, vlm_error = vlm_analyze(
            vlm_cfg, item, transcript, description, frame_paths, lang=lang
        )

    uyum = vlm.get("baslik_icerik_uyumu") if vlm else heu["uyum_skor"]
    gercek = (vlm or {}).get("gerceklik", {}).get("skor") if vlm else None
    karar, genel = _decision(uyum, gercek)

    return {
        "video_id": item["id"],
        "title": item.get("title", ""),
        "is_short": bool(item.get("is_short")),
        "kare_sayisi": len(frame_paths),
        "heuristik": heu,
        "vlm": vlm,
        "vlm_hata": "" if vlm else vlm_error,
        "skorlar": {"baslik_icerik": uyum, "gerceklik": gercek, "genel": genel},
        "karar": karar,
        "ozet": (vlm or {}).get("ozet", ""),
    }
