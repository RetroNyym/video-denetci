"""Transkript + açıklama çekme (video İNDİRMEZ; yalnızca metin/altyazı).

Birincil yol: yt-dlp'nin altyazı akışı (json3/vtt) — YouTube'un
`api/timedtext` endpoint'i doğrudan istekte `pot` (BotGuard) token'ı
istediği için ham baseUrl istekleri 0 bayt döner; yt-dlp bu isteği kendi
innerTube akışı üzerinden çözer.

Yedek yol: watch sayfasındaki `ytInitialPlayerResponse` captionTracks
(herkes için çalışmayabilir).
"""

from __future__ import annotations

import json
import re
import time

import requests
import yt_dlp

from . import config


class WatchError(RuntimeError):
    pass


_last_call = 0.0


def _get(url: str, params: dict | None = None) -> str:
    """Politik gecikmeli, tekrar denemeli GET."""
    global _last_call
    for attempt in range(config.REQUEST_RETRIES + 1):
        wait = config.POLITE_DELAY - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = requests.get(
                url, params=params, headers=config.HEADERS, timeout=config.REQUEST_TIMEOUT
            )
            _last_call = time.time()
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (429, 503) and attempt < config.REQUEST_RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            raise WatchError(f"HTTP {resp.status_code} -> {url}")
        except requests.RequestException as exc:
            if attempt == config.REQUEST_RETRIES:
                raise WatchError(f"Ağ hatası: {exc}") from exc
            time.sleep(1.5 * (attempt + 1))
    raise WatchError("İstek başarısız")


def _get_bytes(url: str) -> bytes:
    global _last_call
    wait = config.POLITE_DELAY - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = requests.get(url, headers=config.HEADERS, timeout=config.REQUEST_TIMEOUT)
        _last_call = time.time()
        return resp.content if resp.status_code == 200 else b""
    except requests.RequestException:
        return b""


# --------------------------------------------------------------------------
# altyazı ayrıştırma
# --------------------------------------------------------------------------

def _json3_to_text(blob: bytes) -> str:
    try:
        data = json.loads(blob.decode("utf-8", errors="ignore"))
    except ValueError:
        return ""
    chunks: list[str] = []
    for event in data.get("events", []):
        segments = event.get("segs")
        if segments:
            chunks.append("".join(seg.get("utf8", "") for seg in segments))
    return " ".join(" ".join(chunks).split())


def _vtt_to_text(blob: bytes) -> str:
    raw = blob.decode("utf-8", errors="ignore")
    lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or line.startswith(("NOTE", "Kind:", "Language:")):
            continue
        if "-->" in line or line.isdigit():
            continue
        # VTT yuvarlanan altyazılarda aynı satırı tekrar eder
        if not lines or lines[-1] != line:
            lines.append(line)
    return " ".join(" ".join(lines).split())


def _pick_lang(track_map: dict, lang: str) -> str | None:
    """İstenen dil -> önce tam eşleşme, sonra alt kod (tr-*, en-*), sonra en."""
    if not isinstance(track_map, dict) or not track_map:
        return None
    keys = list(track_map)
    for key in keys:
        if key == lang:
            return key
    for key in keys:
        if key.startswith(f"{lang}-") or key.startswith(f"{lang}."):
            return key
    for key in keys:
        if key == "en" or key.startswith("en-"):
            return key
    return keys[0]


def _sub_entries_text(entries: list, lang: str) -> str:
    """Bir dilin altyazı formatlarını dener: json3 -> vtt -> diğerleri."""
    if not isinstance(entries, list):
        return ""
    by_ext = {e.get("ext"): e for e in entries if isinstance(e, dict) and e.get("url")}
    for ext in ("json3", "vtt", "srv3", "srv1", "ttml", "srt"):
        entry = by_ext.get(ext)
        if not entry:
            continue
        blob = _get_bytes(entry["url"])
        if not blob:
            continue
        text = _json3_to_text(blob) if ext == "json3" else (
            _vtt_to_text(blob) if ext == "vtt" else _vtt_to_text(blob)
        )
        if text:
            return text
    # hiçbiri worked mi: ilk geçerli url'i dene
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("url"):
            continue
        blob = _get_bytes(entry["url"])
        if blob:
            text = _json3_to_text(blob) or _vtt_to_text(blob)
            if text:
                return text
    return ""


# --------------------------------------------------------------------------
# birincil yol: yt-dlp
# --------------------------------------------------------------------------

def _fetch_ytdlp(video_id: str, lang: str) -> dict | None:
    try:
        with yt_dlp.YoutubeDL(
            {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
        ) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
    except Exception:
        return None
    if not info:
        return None

    description = info.get("description") or ""
    try:
        length = int(info.get("duration") or 0)
    except (TypeError, ValueError):
        length = 0

    transcript, language, kind = "", "", ""
    manual = info.get("subtitles") or {}
    key = _pick_lang(manual, lang)
    if key:
        transcript = _sub_entries_text(manual.get(key) or [], lang)
        if transcript:
            language = key
    if not transcript:
        auto = info.get("automatic_captions") or {}
        key = _pick_lang(auto, lang)
        if key:
            transcript = _sub_entries_text(auto.get(key) or [], lang)
            if transcript:
                language, kind = key, "asr"

    return {
        "title": info.get("title") or "",
        "transcript": transcript,
        "language": language,
        "kind": kind,
        "description": description,
        "length_seconds": length,
    }


# --------------------------------------------------------------------------
# yedek yol: watch sayfası HTML'i
# --------------------------------------------------------------------------

def _extract_json(html: str, marker: str) -> dict:
    """HTML içinde `marker = { ... }` bloğunu dengeli süslü parantez ile ayırır."""
    idx = html.find(marker)
    if idx < 0:
        raise WatchError(f"{marker} bulunamadı")
    start = html.find("{", idx)
    if start < 0:
        raise WatchError(f"{marker} için JSON başlangıcı yok")
    depth = 0
    in_str = False
    escape = False
    for pos in range(start, len(html)):
        ch = html[pos]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = html[start : pos + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError as exc:
                    raise WatchError(f"JSON parse hatası: {exc}") from exc
    raise WatchError(f"{marker} JSON'u kapatılamadı")


def _player_response(html: str) -> dict:
    try:
        return _extract_json(html, "ytInitialPlayerResponse = ")
    except WatchError:
        match = re.search(r"ytInitialPlayerResponse\s*=\s*(\{)", html)
        if not match:
            raise WatchError("ytInitialPlayerResponse yok (video gizli/silinmiş olabilir)")
        return _extract_json(html[match.start() :], "ytInitialPlayerResponse = ")


def _choose_track(tracks: list[dict], lang: str) -> dict | None:
    for track in tracks:                      # istenen dil
        if str(track.get("languageCode", "")).startswith(lang):
            return track
    for track in tracks:                      # elle yazılmış altyazı
        if not track.get("kind"):
            return track
    for track in tracks:                      # otomatik ASR
        if track.get("kind") == "asr":
            return track
    return tracks[0] if tracks else None


def _fetch_html(video_id: str, lang: str) -> dict:
    html = _get(f"https://www.youtube.com/watch?v={video_id}")
    player = _player_response(html)
    details = player.get("videoDetails") or {}
    description = details.get("shortDescription", "") or ""
    try:
        length = int(details.get("lengthSeconds") or 0)
    except (TypeError, ValueError):
        length = 0

    tracks = (
        ((player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
        .get("captionTracks")
        or []
    )
    result = {
        "title": str(details.get("title", "") or ""),
        "transcript": "",
        "language": "",
        "kind": "",
        "description": description,
        "length_seconds": length,
    }
    chosen = _choose_track(tracks, lang) if tracks else None
    if not chosen:
        return result

    base_url = chosen.get("baseUrl")
    if not base_url:
        return result
    blob = _get_bytes(f"{base_url}&fmt=json3") if "?" in base_url else b""
    if blob:
        result["transcript"] = _json3_to_text(blob)
    if not result["transcript"]:
        raw = _get(base_url)
        result["transcript"] = _vtt_to_text(raw.encode("utf-8", errors="ignore"))
    if result["transcript"]:
        result["language"] = str(chosen.get("languageCode", ""))
        result["kind"] = str(chosen.get("kind", ""))
    return result


# --------------------------------------------------------------------------
# kanal araması (sonuç sayfası JSON'u; API anahtarı gerekmez)
# --------------------------------------------------------------------------

def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _simple_text(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "simpleText" in node:
            return node["simpleText"]
        if "runs" in node:
            return "".join(r.get("text", "") for r in node["runs"])
        for key in ("text", "content", "displayName"):
            if isinstance(node.get(key), str):
                return node[key]
    return ""


def search_channels(query: str, limit: int = 8) -> list[dict]:
    """YouTube'da kanal araması yapar (yalnızca sonuç sayfası HTML'i okunur).

    Sonuçlar üç farklı arayüz biçiminden toplanır (eski `channelRenderer`,
    yeni `channelViewModel`, `lockupViewModel` kanal tipi):
      [{"title", "channel_id", "url", "meta"}]
    """
    query = (query or "").strip()
    if not query:
        return []
    html = _get(
        "https://www.youtube.com/results",
        params={"search_query": query, "sp": "EgIQAg==", "hl": "tr", "gl": "TR"},
    )
    try:
        data = _extract_json(html, "var ytInitialData = ")
    except WatchError:
        return []

    out: list[dict] = []
    seen: set[str] = set()

    def _push(channel_id: str, title: str, meta: str = "") -> None:
        if not channel_id.startswith("UC") or channel_id in seen:
            return
        seen.add(channel_id)
        out.append(
            {
                "title": title or channel_id,
                "channel_id": channel_id,
                "url": f"https://www.youtube.com/channel/{channel_id}",
                "meta": meta,
            }
        )

    for node in _walk(data):
        if len(out) >= limit:
            break
        cr = node.get("channelRenderer")
        if isinstance(cr, dict):
            meta = _simple_text(cr.get("videoCountText")) or _simple_text(
                cr.get("subscriberCountText")
            )
            _push(cr.get("channelId", ""), _simple_text(cr.get("title")), meta)
            continue
        cvm = node.get("channelViewModel")
        if isinstance(cvm, dict):
            title = _simple_text(cvm.get("displayName")) or _simple_text(cvm.get("title"))
            meta = _simple_text(cvm.get("videoCountText")) or _simple_text(
                cvm.get("subscriberCountText")
            )
            _push(cvm.get("channelId", ""), title, meta)
            continue
        lm = node.get("lockupViewModel")
        if isinstance(lm, dict) and str(lm.get("contentType", "")).endswith("_CHANNEL"):
            title = ""
            meta_data = lm.get("metadata")
            if isinstance(meta_data, dict):
                title = _simple_text(
                    (meta_data.get("lockupMetadataViewModel") or {}).get("title")
                )
            _push(lm.get("contentId", ""), title, "")
    return out


# --------------------------------------------------------------------------
# izlenme sayacı okuma (yalnızca OKUMA — sayaç üretimi/şişirme yok)
# --------------------------------------------------------------------------

def view_count(video_id: str) -> int | None:
    """Sayfanın gösterdiği **tam** izlenme sayısını okur; alınamazsa None.

    `ytInitialPlayerResponse.videoDetails.viewCount` tam sayıdır; arayüzdeki
    "1,2 mn" gibi yuvarlanmış metin kullanılmaz. Ağ/parse hatasında None
    döner — çağrı yapan izlemeyi asla durdurmaz.
    """
    try:
        html = _get(f"https://www.youtube.com/watch?v={video_id}")
    except WatchError:
        return None
    match = re.search(r'"viewCount"\s*:\s*"(\d+)"', html)
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------

def fetch(video_id: str, lang: str = "tr") -> dict:
    """Başlık + transkript + açıklama + süre; altyazı yoksa transkript boş döner.

    Dönen sözlük:
      {"title", "transcript", "language", "kind", "description", "length_seconds"}
    """
    data = _fetch_ytdlp(video_id, lang)
    if data is None:
        data = _fetch_html(video_id, lang)
    return data


def save_text_cache(video_id: str, data: dict) -> None:
    """Çekilen metinleri `out/<id>/` altına yazar (boş içerik mevcut dosyayı silmez)."""
    folder = config.OUT / video_id
    folder.mkdir(parents=True, exist_ok=True)
    tp, dp = folder / "transkript.txt", folder / "aciklama.txt"
    transcript = data.get("transcript") or ""
    description = data.get("description") or ""
    if transcript or not tp.exists():
        tp.write_text(transcript, encoding="utf-8")
    if description or not dp.exists():
        dp.write_text(description, encoding="utf-8")

