"""Kanal taraması: /videos ve /shorts sekmeleri (devam sayfaları dahil).

yt-dlp yalnızca liste (flat) modunda çalışır; **hiçbir video indirilmez**,
yalnızca metadata alınır. Shorts sekmesi ayrı çekildiği için kısa videolar
taramadan kaçmaz; her iki sekmede görülen video tekilleştirilir.
"""

from __future__ import annotations

import re

import yt_dlp

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_VIDEO_TARGET_RE = re.compile(
    r"(youtube\.com/(watch\?|shorts/|live/|embed/)|youtu\.be/)", re.I
)


def is_video_target(target: str) -> bool:
    """Hedef tek bir video mu, kanal mı?"""
    value = target.strip()
    return bool(_VIDEO_TARGET_RE.search(value)) or bool(VIDEO_ID_RE.fullmatch(value))


def video_item(target: str) -> dict:
    """Tek video hedefinden kayıt üretir."""
    value = target.strip()
    vid_match = re.search(r"(?:v=|shorts/|live/|embed/|youtu\.be/)([A-Za-z0-9_-]{11})", value)
    vid = vid_match.group(1) if vid_match else value
    return {
        "id": vid,
        "title": "",
        "url": f"https://www.youtube.com/watch?v={vid}",
        "duration": None,
        "is_short": "/shorts/" in value,
    }


def normalize_channel(target: str) -> str:
    """@handle, UC... ID ya da tam URL'yi kanal adresine çevirir."""
    value = target.strip().rstrip("/")
    if value.startswith("http"):
        return value
    if value.startswith("@"):
        return f"https://www.youtube.com/{value}"
    if value.startswith("UC") and len(value) == 24:
        return f"https://www.youtube.com/channel/{value}"
    return f"https://www.youtube.com/@{value.lstrip('@')}"


def collect(target: str, limit: int = 0, tabs: tuple[str, ...] = ("videos", "shorts")) -> list[dict]:
    """Kanalın sekmelerini sırayla çeker; devam sayfaları dahil tüm liste.

    - limit > 0 ise her sekme için ilk `limit` kayıt.
    - Sekmeler tekilleştirilir: /shorts'ta görünen video is_short = True.
    - Süre <= 60 sn olan ve yalnızca /videos'ta görünenler de short kabul edilir
      (eski Shorts kuralı; /shorts sekmesinde görünmeyen kısa klipler için).
    """
    base = normalize_channel(target)
    seen: dict[str, dict] = {}
    for tab in tabs:
        opts = {
            "extract_flat": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
        }
        if limit and limit > 0:
            opts["playlistend"] = limit
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{base}/{tab}", download=False)
        for entry in (info or {}).get("entries") or []:
            if not entry:
                continue
            vid = entry.get("id") or ""
            if not VIDEO_ID_RE.match(vid):
                continue  # playlist / raf satırı vb.
            item = seen.get(vid) or {
                "id": vid,
                "title": entry.get("title") or "",
                "url": f"https://www.youtube.com/watch?v={vid}",
                "duration": entry.get("duration"),
                "is_short": False,
            }
            if tab == "shorts":
                item["is_short"] = True
            if not item["title"] and entry.get("title"):
                item["title"] = entry.get("title") or ""
            seen[vid] = item

    for item in seen.values():
        dur = item.get("duration")
        if not item["is_short"] and isinstance(dur, (int, float)) and dur and dur <= 60:
            item["is_short"] = True
    return list(seen.values())
