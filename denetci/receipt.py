"""İzleme belgesi (proof of watch) — "izlendi" ifadesini kanıtlanabilir kılar.

Her başarılı izlemede `out/<id>/izleme_belgesi.json` yazılır:

  - zaman damgası (UTC), oynatılan süre / video süresi, mod, çözünürlük, sayfa
  - her karenin SHA-256 özeti + boyut + yakalama anı (dosya mtime, UTC)
  - transkript önbelleğinin SHA-256 özeti
  - belgenin kendisi için **içerik özeti** (SHA-256, kanonik JSON)

`verify` bu özeti yeniden hesaplar ve kare dosyalarını tek tek karşılaştırır;
belgede ya da karelerde en küçük bir değişiklik özet uyuşmazlığı verir. Böylece
iddia/denetim süreçlerinde "bu video şu tarihte oynatıldı ve denetlendi"
iddiası üçüncü taraflarca doğrulanabilir.

Sınır: araç YouTube'da izlenme/etkileşim metriği üretmez; belge yalnızca
aracın kendi kaydıdır ve yerel olarak imzalanmaz — bütünlük, yeniden
hesaplanabilir içerik özetiyle korunur.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, config

RECEIPT_NAME = "izleme_belgesi.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(doc: dict) -> str:
    """Belge içeriğinin kanonik özeti (`belge_ozeti` alanı hariç)."""
    payload = {k: v for k, v in doc.items() if k != "belge_ozeti"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create(row: dict, res: dict) -> dict | None:
    """İzleme sonucundan belge üretir: {"yol": Path, "ozet": str} ya da None."""
    vid = row["id"]
    folder = config.OUT / vid
    frames_dir = folder / "kareler"

    kareler: list[dict] = []
    if frames_dir.exists():
        for p in sorted(frames_dir.glob("kare_*.jpg")):
            st = p.stat()
            kareler.append(
                {
                    "dosya": f"kareler/{p.name}",
                    "sha256": _sha256_file(p),
                    "boyut": st.st_size,
                    "yakalama": datetime.fromtimestamp(
                        st.st_mtime, timezone.utc
                    ).isoformat(timespec="seconds"),
                }
            )

    tp = folder / "transkript.txt"
    tp_ok = tp.exists() and tp.stat().st_size > 0

    doc = {
        "arac": "video-denetci",
        "surum": __version__,
        "video": {
            "id": vid,
            "url": row.get("url") or res.get("url_used") or "",
            "baslik": row.get("title") or "",
            "kanal": row.get("channel") or "",
            "shorts": bool(row.get("is_short")),
        },
        "izleme": {
            "zaman": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mod": res.get("mode") or "",
            "oynanan_saniye": res.get("watched") or 0,
            "video_saniye": res.get("duration") or row.get("duration") or 0,
            "cozunurluk": res.get("resolution") or "",
            "sayfa": res.get("url_used") or "",
            "kare_sayisi": len(kareler),
        },
        "kareler": kareler,
        "transkript": {
            "var": bool(tp_ok),
            "sha256": _sha256_file(tp) if tp_ok else "",
        },
    }
    doc["belge_ozeti"] = _digest(doc)

    path = folder / RECEIPT_NAME
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return None
    return {"yol": path, "ozet": doc["belge_ozeti"]}


def verify(row: dict) -> dict:
    """Bir kaydın belgesini doğrular → {"video_id", "durum", "not", ...}.

    durum: "ok" (bütünlük sağlam) · "bozuk" · "yok" (bu izleme belgesiz yapıldı)
    """
    vid = row["id"]
    path = config.OUT / vid / RECEIPT_NAME
    out = {"video_id": vid, "durum": "yok", "not": "", "belge_ozeti": "", "zaman": ""}
    if not path.exists():
        out["not"] = "belge yok (izleme, belge özelliği eklendikten önce yapıldı)"
        return out
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        out["durum"] = "bozuk"
        out["not"] = "belge okunamadı / JSON bozuk"
        return out
    if not isinstance(doc, dict):
        out["durum"] = "bozuk"
        out["not"] = "belge biçimi hatalı"
        return out

    out["belge_ozeti"] = str(doc.get("belge_ozeti") or "")
    iz = doc.get("izleme") or {}
    out["zaman"] = str(iz.get("zaman") or "")

    if _digest(doc) != doc.get("belge_ozeti"):
        out["durum"] = "bozuk"
        out["not"] = "belge özeti uyuşmuyor (belge değiştirilmiş)"
        return out

    base = config.OUT / vid
    problems: list[str] = []
    for k in doc.get("kareler") or []:
        fp = base / str(k.get("dosya") or "")
        if not fp.exists():
            problems.append(f"eksik kare: {k.get('dosya')}")
        elif _sha256_file(fp) != k.get("sha256"):
            problems.append(f"kare özeti uyuşmuyor: {k.get('dosya')}")
    if problems:
        out["durum"] = "bozuk"
        out["not"] = "; ".join(problems[:3])
        return out

    out["durum"] = "ok"
    out["not"] = (
        f"{len(doc.get('kareler') or [])} kare doğrulandı · "
        f"{iz.get('oynanan_saniye', 0)} sn oynandı · mod {iz.get('mod', '?')}"
    )
    return out


def verify_all(rows: list[dict]) -> list[dict]:
    return [verify(r) for r in rows]
