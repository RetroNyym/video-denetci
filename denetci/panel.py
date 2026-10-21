"""Yerel web paneli (GUI) — 127.0.0.1 üzerinde tek sayfa arayüz.

    python -m denetci panel [--port 8788] [--no-browser]

Tarayıcıda açılan arayüz:
  - hedef + seçenekler formu (tarama / izleme / denetim / rapor / hepsi)
  - canlı ilerleme günlüğü ve faz göstergesi
  - video tablosu: kare thumbnail'ı, tip, durum, skorlar, karar rozeti
  - VLM ayarları (OpenAI uyumlu uç nokta)
  - hazır HTML rapora bağlantı (/reports/latest.html)

Arka planda tek iş parçacığı çalışır; HTTP sunucusu ThreadingHTTPServer'dır.
Tüm ara işlevler CLI'deki ile aynı modülleri kullanır (collector/watcher/...).
"""

from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote

from . import analyzer, collector, config, receipt, report, store, watchpage, watcher

_SETTINGS_FILE = config.DATA / "panel_settings.json"

DEFAULT_SETTINGS: dict = {
    "target": "",
    "limit": 0,
    "tabs": "videos,shorts",
    "frames": 0,
    "realtime": False,
    "headed": False,
    "retry": False,
    "lang": "tr",
    "vlm_base": "",
    "vlm_key": "",
    "vlm_model": "",
    "vlm_frames": 6,
}

_PHASE_NAMES = {
    "scan": "tarama",
    "watch": "izleme",
    "verify": "belge doğrulama",
    "anchor": "çapalama",
    "analyze": "denetim",
    "report": "rapor",
    "run": "tam akış",
}


# ----------------------------------------------------------------- ayarlar
def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            data.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return data


def save_settings(update: dict) -> dict:
    data = load_settings()
    for key, value in (update or {}).items():
        if key in DEFAULT_SETTINGS:
            data[key] = value
    config.ensure_dirs()
    try:
        _SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
    return data


# --------------------------------------------------------------- iş durumu
class _Job:
    """Tek iş parçacıklı iş yöneticisi + kilitli durum alanı."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.busy = False
        self.phase = "hazır"
        self.lines: list[str] = []

    def begin(self, phase: str) -> bool:
        with self.lock:
            if self.busy:
                return False
            self.busy = True
            self.phase = phase
            self.lines = []
            return True

    def log(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            if len(self.lines) > 400:
                del self.lines[:-300]

    def end(self, phase: str) -> None:
        with self.lock:
            self.busy = False
            self.phase = phase

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "busy": self.busy,
                "phase": self.phase,
                "lines": list(self.lines[-200:]),
            }


JOB = _Job()


# ------------------------------------------------------------- iş fonksiyonu
def _vlm_cfg(st: dict) -> dict | None:
    base = (st.get("vlm_base") or "").strip()
    if not base:
        return None
    model = (st.get("vlm_model") or "").strip() or "gpt-4o-mini"
    return {
        "base": base,
        "key": (st.get("vlm_key") or "").strip(),
        "model": model,
        "max_frames": int(st.get("vlm_frames") or config.VLM_MAX_FRAMES),
    }


def _tabs_of(st: dict) -> tuple[str, ...]:
    raw = str(st.get("tabs") or "videos,shorts")
    tabs = tuple(t.strip() for t in raw.split(",") if t.strip() in ("videos", "shorts"))
    return tabs or ("videos", "shorts")


def _do_scan(st: dict) -> None:
    target = (st.get("target") or "").strip()
    if not target:
        raise RuntimeError("hedef boş — arama çubuğundan kanal seçin ya da @kanal girin")
    tabs = _tabs_of(st)
    limit = int(st.get("limit") or 0)
    JOB.log(f"Taranıyor: {target}  (sekmeler: {', '.join(tabs)}, limit: {limit or 'tümü'})")
    if collector.is_video_target(target):
        items = [collector.video_item(target)]
        channel = ""
        # tek videoda başlık + metinleri hemen çek (analiz önbelleği de dolar)
        try:
            data = watchpage.fetch(items[0]["id"], lang=str(st.get("lang") or "tr"))
            if data.get("title"):
                items[0]["title"] = data["title"]
            watchpage.save_text_cache(items[0]["id"], data)
            JOB.log(f"Başlık: {items[0]['title'] or '(başlık yok)'}")
        except watchpage.WatchError as exc:
            JOB.log(f"Başlık alınamadı: {exc}")
    else:
        items = collector.collect(target, limit=limit, tabs=tabs)
        channel = target
    if not items:
        raise RuntimeError("video bulunamadı (kanal yeni/gizli ya da URL hatalı)")
    store.upsert_videos(items, channel=channel)
    n_short = sum(1 for i in items if i.get("is_short"))
    JOB.log(f"{len(items)} kayıt eklendi ({n_short} Shorts, {len(items) - n_short} video)")
    JOB.log(f"Durum: {store.counts()}")


def _do_watch(st: dict) -> None:
    realtime = bool(st.get("realtime"))
    headless = not bool(st.get("headed"))     # "pencerede göster" işaretliyse görünür tarayıcı
    frames = int(st.get("frames") or 0) or None
    pending = store.rows(("listed",))
    if st.get("retry"):
        pending += store.rows("error")
    if not pending:
        JOB.log("İzlenecek yeni video yok (durum: listed).")
        return
    mode = "gerçek zamanlı" if realtime else "örneklem"
    JOB.log(f"{len(pending)} video oynatılacak ({mode} mod, "
            f"{'başlıklı pencere' if not headless else 'gizli pencere'})")
    ok = err = 0
    for i, row in enumerate(pending, 1):
        tag = "Shorts" if row["is_short"] else "video"
        JOB.log(f"({i}/{len(pending)}) [{tag}] {(row['title'] or row['id'])[:60]}")
        sayac_on = watchpage.view_count(row["id"])   # izlemeden önce sayaç (okuma)
        try:
            res = watcher.watch_video(
                row, headless=headless, realtime=realtime, frames=frames
            )
        except Exception as exc:  # noqa: BLE001 — tek video hatası işi durdurmasın
            store.set_fields(row["id"], status="error", error=f"izleme: {exc}")
            JOB.log(f"  HATA: {exc}")
            err += 1
            continue
        if res.get("error") and not res.get("frames"):
            store.set_fields(row["id"], status="error", error=res["error"])
            JOB.log(f"  HATA: {res['error']}")
            err += 1
            continue
        store.set_fields(
            row["id"],
            status="watched",
            folder=str(config.OUT / row["id"]),
            frames=len(res["frames"]),
            mode=res["mode"],
            duration=res.get("duration") or row.get("duration") or 0,
            error=res.get("error", ""),
        )
        JOB.log(f"  {len(res['frames'])} kare · {res.get('duration', 0)} sn · {res['mode']}")
        res["sayac_oncesi"] = sayac_on
        if sayac_on is not None:
            time.sleep(5)   # sayaç anında güncellenmeyebilir; kısa bekleme
            res["sayac_sonrasi"] = watchpage.view_count(row["id"])
        belge = receipt.create(row, res)
        if belge:
            JOB.log(f"  izleme belgesi: izleme_belgesi.json · özet {belge['ozet'][:16]}…")
        if isinstance(res.get("sayac_oncesi"), int) and isinstance(res.get("sayac_sonrasi"), int):
            JOB.log(f"  YouTube sayacı: {res['sayac_oncesi']} → {res['sayac_sonrasi']} "
                    f"({res['sayac_sonrasi'] - res['sayac_oncesi']:+d})")
        ok += 1
    JOB.log(f"İzleme bitti: {ok} başarılı, {err} hata.")


def _do_verify() -> None:
    rows = store.rows(("watched", "analyzed", "error"))
    if not rows:
        JOB.log("Doğrulanacak kayıt yok. Önce `İzleme` çalıştırın.")
        return
    results = receipt.verify_all(rows)
    ok = 0
    for r in results:
        mark = {"ok": "✓", "bozuk": "✗", "yok": "–"}[r["durum"]]
        JOB.log(f"{mark} {r['video_id']} · {r['durum']} · {r['not']}")
        if r["belge_ozeti"]:
            JOB.log(f"    özet {r['belge_ozeti'][:24]}… · {r['zaman'] or '?'}")
        if r["durum"] == "ok":
            ok += 1
    n_bozuk = sum(1 for r in results if r["durum"] == "bozuk")
    n_yok = sum(1 for r in results if r["durum"] == "yok")
    JOB.log(f"Belge doğrulama: {ok} doğrulandı, {n_bozuk} bozuk, {n_yok} belgesiz.")


def _do_anchor() -> None:
    rows = store.rows(("watched", "analyzed", "error"))
    dest = config.ROOT / receipt.ANCHOR_DIR_NAME
    copied = receipt.stage_anchor(rows, dest)
    if not copied:
        JOB.log("Çapalanacak belge yok. Önce `İzleme` çalıştırın.")
        return
    JOB.log(f"{len(copied)} belge → belgeler/ (git commit + push)")
    durum, mesaj = receipt.anchor_to_git(dest, copied)
    if durum == "hata":
        JOB.log(f"HATA: {mesaj}")
        raise RuntimeError(mesaj)
    JOB.log(mesaj)


def _do_analyze(st: dict) -> None:
    statuses: tuple[str, ...] = ("watched",)
    if st.get("retry"):
        statuses = ("watched", "analyzed", "error")
    pending = store.rows(statuses)
    if not pending:
        JOB.log("Denetlenecek video yok (durum: watched).")
        return
    vlm_cfg = _vlm_cfg(st)
    base_lang = str(st.get("lang") or "tr")
    if vlm_cfg:
        JOB.log(f"{len(pending)} video denetlenecek · VLM: {vlm_cfg['base']} ({vlm_cfg['model']})")
    else:
        JOB.log(f"{len(pending)} video denetlenecek · yalnız anahtarsız katman "
                "(gerçeklik için VLM ayarlayın)")
    ok = err = 0
    for i, row in enumerate(pending, 1):
        vid = row["id"]
        folder = config.OUT / vid
        JOB.log(f"({i}/{len(pending)}) {(row['title'] or vid)[:60]}")
        transcript = description = ""
        lang = ""
        tp, dp = folder / "transkript.txt", folder / "aciklama.txt"
        # önbellek yeterliyse (transkript var + başlık biliniyor) doğrudan kullan
        if tp.exists() and tp.stat().st_size > 0 and row.get("title"):
            transcript = tp.read_text(encoding="utf-8", errors="ignore")
            description = dp.read_text(encoding="utf-8", errors="ignore") if dp.exists() else ""
        else:
            # altyazı dilini başlık diline göre seç; başlık eksikse bu çağrı onu da getirir
            desc_hint = ""
            if dp.exists():
                desc_hint = dp.read_text(encoding="utf-8", errors="ignore")[:400]
            prefer = analyzer.detect_lang(row.get("title", ""), desc_hint) or base_lang
            try:
                data = watchpage.fetch(vid, lang=prefer)
                transcript = data["transcript"]
                description = data["description"]
                lang = data["language"]
                if data.get("title") and not row.get("title"):
                    store.set_fields(vid, title=data["title"])
                    row["title"] = data["title"]
                    JOB.log(f"  başlık: {data['title'][:60]}")
                watchpage.save_text_cache(vid, data)
            except watchpage.WatchError as exc:
                JOB.log(f"  transkript alınamadı: {exc}")
        frames = sorted(str(p) for p in (folder / "kareler").glob("kare_*.jpg"))
        try:
            analysis = analyzer.analyze(
                row, transcript, description, frames, vlm_cfg=vlm_cfg, lang=base_lang
            )
        except Exception as exc:  # noqa: BLE001
            store.set_fields(vid, status="error", error=f"analiz: {exc}")
            JOB.log(f"  HATA: {exc}")
            err += 1
            continue
        (folder / "analiz.json").write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        store.set_fields(
            vid,
            status="analyzed",
            analysis=analysis,
            transcript_lang=lang or row.get("transcript_lang", ""),
            error="",
        )
        s = analysis["skorlar"]
        JOB.log(f"  {analysis['karar']} · genel {s['genel']} "
                f"(uyum {s['baslik_icerik']}, gerçeklik {s['gerceklik']})")
        ok += 1
    JOB.log(f"Denetim bitti: {ok} başarılı, {err} hata.")


def _do_report() -> None:
    rows = store.rows("analyzed")
    if not rows:
        JOB.log("Denetlenmiş kayıt yok — rapor atlandı.")
        return
    path = report.build(rows, channel=_channel_of(rows))
    JOB.log(f"Rapor hazır: {path}")
    JOB.log("Panelde 'Raporu aç' butonu veya /reports/latest.html")


def _channel_of(rows: list[dict]) -> str:
    for row in rows:
        if row.get("channel"):
            return row["channel"]
    return ""


def _runner(action: str, st: dict) -> bool:
    """İşi arka planda başlatır; meşgulse False döner."""
    phase = _PHASE_NAMES.get(action, action)
    if not JOB.begin(phase):
        return False

    def worker() -> None:
        try:
            if action in ("scan", "run"):
                _do_scan(st)
            if action in ("watch", "run"):
                _do_watch(st)
            if action in ("analyze", "run"):
                _do_analyze(st)
            if action in ("report", "run"):
                _do_report()
            if action == "verify":
                _do_verify()
            if action == "anchor":
                _do_anchor()
            JOB.log("— tamamlandı —")
            JOB.end("bitti")
        except Exception as exc:  # noqa: BLE001 — iş hatası arayüzü düşürmez
            JOB.log(f"HATA: {exc}")
            JOB.end("hata")

    threading.Thread(target=worker, daemon=True).start()
    return True


# ------------------------------------------------------------------ payload
def _state_payload() -> dict:
    return {
        **JOB.snapshot(),
        "counts": store.counts(),
        "report": (config.REPORTS / "latest.html").exists(),
        "settings": load_settings(),
    }


def _videos_payload() -> list[dict]:
    out: list[dict] = []
    for r in store.rows():
        frames_dir = config.OUT / r["id"] / "kareler"
        thumb = ""
        if frames_dir.exists():
            frames = sorted(frames_dir.glob("kare_*.jpg"))
            if frames:
                thumb = f"/out/{r['id']}/kareler/{frames[0].name}"
        a = r.get("analysis") or {}
        s = (a.get("skorlar") or {}) if a else {}
        belge = ""
        sayac = ""
        bp = config.OUT / r["id"] / receipt.RECEIPT_NAME
        if bp.exists():
            try:
                doc = json.loads(bp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = None
            if not isinstance(doc, dict):
                belge = "bozuk"
            else:
                belge = str(doc.get("belge_ozeti") or "")[:16]
                sc = (doc.get("izleme") or {}).get("sayac") or {}
                if isinstance(sc.get("oncesi"), int) and isinstance(sc.get("sonrasi"), int):
                    sayac = (f"{sc['oncesi']} → {sc['sonrasi']} "
                             f"({sc['sonrasi'] - sc['oncesi']:+d})")
        out.append(
            {
                "id": r["id"],
                "title": r.get("title") or (a.get("title") if a else "") or r["id"],
                "url": r.get("url") or "",
                "is_short": bool(r.get("is_short")),
                "status": r.get("status"),
                "error": r.get("error") or "",
                "frames": r.get("frames") or 0,
                "transcript_lang": r.get("transcript_lang") or "",
                "thumb": thumb,
                "uyum": s.get("baslik_icerik"),
                "gerceklik": s.get("gerceklik"),
                "genel": s.get("genel"),
                "karar": (a.get("karar", "") if a else ""),
                "ozet": (a.get("ozet", "") if a else ""),
                "vlm_hata": (a.get("vlm_hata", "") if a else ""),
                "belge": belge,
                "sayac": sayac,
            }
        )

    def severity(item: dict) -> tuple:
        if item["status"] != "analyzed":
            return (4, 999, item["title"])
        k = (item["karar"] or "").lower()
        if k.startswith(("güvensiz", "uyumsuz")):
            sev = 0
        elif k.startswith("şüpheli"):
            sev = 1
        elif k.startswith(("güvenilir", "uyumlu")):
            sev = 2
        else:
            sev = 3
        return (sev, item["genel"] if item["genel"] is not None else 999, item["title"])

    out.sort(key=severity)
    return out


# ---------------------------------------------------------------- HTTP sunucu
_CTYPE = {
    ".html": "text/html; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "denetci-panel/0.1"

    def log_message(self, *args) -> None:  # noqa: A003 — HTTP gürültüsünü kapat
        pass

    # --- yardımcılar -------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code: int = 200) -> None:
        self._send(
            code,
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            _CTYPE[".json"],
        )

    def _serve_under(self, url_path: str, root: Path) -> None:
        """Güvenli dosya servisi (kök dışı erişim reddedilir)."""
        root = root.resolve()
        target = (root / unquote(url_path)).resolve()
        root_str, target_str = str(root), str(target)
        # containment kontrolü: `..` ile kök dışına çıkılamaz
        if target_str != root_str and not target_str.startswith(root_str + os.sep):
            self._send(403, b"forbidden", _CTYPE[".txt"])
            return
        if not target.is_file():
            self._send(404, b"not found", _CTYPE[".txt"])
            return
        try:
            body = target.read_bytes()
        except OSError:
            self._send(404, b"not found", _CTYPE[".txt"])
            return
        ctype = _CTYPE.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, body, ctype)

    # --- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send(200, PANEL_HTML.encode("utf-8"), _CTYPE[".html"])
        elif path == "/api/state":
            self._json(_state_payload())
        elif path == "/api/videos":
            self._json(_videos_payload())
        elif path == "/api/search":
            q = (parse_qs(query).get("q") or [""])[0]
            try:
                items = watchpage.search_channels(q, limit=8)
            except watchpage.WatchError as exc:
                self._json({"ok": False, "error": str(exc)})
                return
            self._json({"ok": True, "items": items})
        elif path.startswith("/out/"):
            self._serve_under(path[len("/out/") :], config.OUT)
        elif path.startswith("/reports/"):
            self._serve_under(path[len("/reports/") :], config.REPORTS)
        else:
            self._send(404, b"not found", _CTYPE[".txt"])

    # --- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                body = {}
        except (ValueError, UnicodeDecodeError):
            self._json({"ok": False, "error": "geçersiz gövde"}, code=400)
            return

        if path == "/api/settings":
            self._json({"ok": True, "settings": save_settings(body)})
        elif path == "/api/run":
            action = str(body.get("action") or "")
            if action not in _PHASE_NAMES:
                self._json({"ok": False, "error": "geçersiz action"}, code=400)
                return
            st = save_settings(body)
            if not _runner(action, st):
                self._json({"ok": False, "error": "iş çalışıyor"}, code=409)
            else:
                self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "bulunamadı"}, code=404)


# ------------------------------------------------------------------- arayüz
PANEL_HTML = """<!doctype html>
<html lang="tr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>video-denetci · panel</title>
<style>
  :root{--ink:#1c2430;--mut:#67717e;--ok:#1a9850;--mid:#f0a202;--bad:#d73027;
        --line:#e3e8ef;--bg:#f4f6f9;--navy:#0f172a}
  *{box-sizing:border-box}
  body{font:15px/1.5 system-ui,Segoe UI,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
  header{background:var(--navy);color:#fff;padding:16px 22px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  header h1{margin:0;font-size:18px}
  .chip{font-size:12px;background:#1e293b;padding:4px 10px;border-radius:999px;color:#cbd5e1}
  .chip b{color:#fff}
  .phase{font-size:13px;font-weight:700;padding:4px 12px;border-radius:999px;background:#334155;color:#cbd5e1}
  .phase.busy{background:#f0a202;color:#1c2430}
  .phase.err{background:var(--bad);color:#fff}
  main{max-width:1180px;margin:0 auto;padding:18px}
  .card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
  input[type=text],input[type=number],select{padding:8px 10px;border:1px solid var(--line);border-radius:8px;font:inherit}
  input[type=text]{min-width:280px;flex:1}
  input[type=number]{width:92px}
  label{font-size:13px;color:var(--mut);display:flex;align-items:center;gap:5px}
  button{padding:9px 16px;border:0;border-radius:8px;font:inherit;font-weight:600;cursor:pointer;background:#e8edf4;color:var(--ink)}
  button.primary{background:#2563eb;color:#fff}
  button.warn{background:#0f172a;color:#fff}
  button:disabled{opacity:.45;cursor:not-allowed}
  details{margin:6px 0;font-size:14px}
  details summary{cursor:pointer;color:var(--mut)}
  #log{background:#0b1220;color:#b9c6d9;font:12px/1.55 Consolas,monospace;padding:12px;border-radius:8px;
       max-height:220px;overflow:auto;white-space:pre-wrap;margin:10px 0 0;display:none}
  table{width:100%;border-collapse:collapse}
  th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);text-align:left;padding:8px;border-bottom:2px solid var(--line)}
  td{padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:middle;font-size:14px}
  td.num{text-align:center}
  .thumb{width:96px;height:54px;object-fit:cover;border-radius:6px;display:block;background:#eef1f6}
  .noimg{width:96px;height:54px;border-radius:6px;background:#eef1f6;display:flex;align-items:center;
         justify-content:center;font-size:11px;color:var(--mut)}
  .type{font-size:10px;font-weight:700;background:#eef1f6;color:var(--mut);padding:3px 6px;border-radius:5px}
  .verdict{font-size:12px;font-weight:700;padding:4px 9px;border-radius:999px;white-space:nowrap}
  .v0{background:#fdecea;color:var(--bad)}.v1{background:#fff6e0;color:#b47700}
  .v2{background:#e8f6ee;color:var(--ok)}.v3{background:#eef1f6;color:var(--mut)}
  .st{font-size:11px;color:var(--mut)}
  .ok{color:var(--ok)}.mid{color:#b47700}.bad{color:var(--bad)}.na{color:#b6bfcc}
  .title a{color:var(--ink);text-decoration:none}
  .title a:hover{text-decoration:underline}
  .empty{padding:26px;text-align:center;color:var(--mut)}
  .muted{color:var(--mut);font-size:13px}
  .sc{font-size:17px;font-weight:700}
  .results{display:flex;flex-wrap:wrap;gap:8px;margin:-2px 0 12px}
  .res{background:#f8fafc;border:1px solid var(--line);padding:7px 12px;border-radius:8px;
       font-size:13px;font-weight:500}
  .res:hover{border-color:#2563eb;color:#2563eb}
  #rows.spin{opacity:.6}
</style></head>
<body>
<header>
  <h1>video-denetci</h1>
  <span id="phase" class="phase">hazır</span>
  <span class="chip">listeli <b id="c-listed">0</b></span>
  <span class="chip">izlendi <b id="c-watched">0</b></span>
  <span class="chip">denetlendi <b id="c-analyzed">0</b></span>
  <span class="chip">hata <b id="c-error">0</b></span>
  <span style="flex:1"></span>
  <a id="reportLink" href="/reports/latest.html" target="_blank"
     style="color:#93c5fd;font-size:13px;display:none">📄 rapor</a>
</header>
<main>
  <section class="card">
    <div class="row">
      <input type="text" id="q" placeholder="🔍 kanal ara (örn. mkbhd, orkun isitmak)">
      <button id="searchBtn">Kanal ara</button>
    </div>
    <div id="results" class="results" style="display:none"></div>
    <div class="row">
      <input type="text" id="target" placeholder="@kanal, kanal URL, UC... ya da video URL">
      <label>limit <input type="number" id="limit" value="0" min="0"></label>
      <label><input type="checkbox" id="tabVideos" checked> /videos</label>
      <label><input type="checkbox" id="tabShorts" checked> /shorts</label>
      <button class="primary" data-run="scan">▶ Tarama</button>
    </div>
    <div class="row">
      <label>kare <input type="number" id="frames" value="0" min="0" max="60"></label>
      <label><input type="checkbox" id="realtime"> gerçek zamanlı izle</label>
      <label><input type="checkbox" id="headed"> izlemeyi pencerede göster</label>
      <label><input type="checkbox" id="retry"> hatalıları/yeniden denet</label>
      <label>dil <select id="lang"><option value="tr">tr</option><option value="en">en</option></select></label>
      <button data-run="watch">▶ İzleme</button>
      <button data-run="analyze">▶ Denetim</button>
      <button data-run="report">📄 Rapor</button>
      <button data-run="verify">🔎 Belge doğrula</button>
      <button data-run="anchor">📌 GitHub'a çapa</button>
      <button class="warn" data-run="run">⚡ Hepsini çalıştır</button>
      <a href="/reports/latest.html" target="_blank"><button id="openReport" disabled>Raporu aç</button></a>
    </div>
    <details id="vlmBox">
      <summary>VLM ayarları (gerçeklik skoru için — OpenAI uyumlu uç nokta)</summary>
      <div class="row" style="margin-top:8px">
        <input type="text" id="vlmBase" placeholder="base: https://api.openai.com/v1 · http://localhost:11434/v1">
        <input type="text" id="vlmKey" placeholder="API anahtarı (boş olabilir)">
        <input type="text" id="vlmModel" placeholder="model (örn. gpt-4o-mini, qwen2.5vl:7b)">
        <label>kare <input type="number" id="vlmFrames" value="6" min="1" max="16"></label>
      </div>
      <div class="muted">Boşsa denetim yalnız anahtarsız katmanla çalışır (gerçeklik “—”).
        Ayarlar kaydolur; alanlar istemci tarafında saklanmaz.</div>
    </details>
    <pre id="log"></pre>
  </section>

  <section class="card" id="rows">
    <table>
      <thead><tr>
        <th>kare</th><th>başlık</th><th>tip</th><th>durum</th>
        <th style="text-align:center">uyum</th><th style="text-align:center">gerçeklik</th>
        <th style="text-align:center">genel</th><th>karar</th>
      </tr></thead>
      <tbody id="tbody"><tr><td colspan="8" class="empty">Henüz kayıt yok — önce <b>Tarama</b> çalıştırın.</td></tr></tbody>
    </table>
  </section>
</main>

<script>
const $ = id => document.getElementById(id);
let busy = false;
let filled = false;   // ayarlar yalnızca bir kez forma doldurulur (yazarken sıfırlanmasın)

function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function verdictClass(k){k=(k||'').toLowerCase();
  if(k.startsWith('güvensiz')||k.startsWith('uyumsuz'))return 'v0';
  if(k.startsWith('şüpheli'))return 'v1';
  if(k.startsWith('güvenilir')||k.startsWith('uyumlu'))return 'v2';
  return 'v3';}
function scoreCell(v){
  if(v===null||v===undefined)return '<span class="na">—</span>';
  const cls=v>=70?'ok':(v>=45?'mid':'bad');
  return '<span class="sc '+cls+'">'+v+'</span>';}
const ST={listed:'listelendi',watched:'izlendi',analyzed:'denetlendi',error:'hata'};

function collect(){
  return {
    target:$('target').value.trim(),
    limit:parseInt($('limit').value||'0',10),
    tabs:[ $('tabVideos').checked?'videos':null, $('tabShorts').checked?'shorts':null ].filter(Boolean).join(',')||'videos,shorts',
    frames:parseInt($('frames').value||'0',10),
    realtime:$('realtime').checked,
    headed:$('headed').checked,
    retry:$('retry').checked,
    lang:$('lang').value,
    vlm_base:$('vlmBase').value.trim(),
    vlm_key:$('vlmKey').value.trim(),
    vlm_model:$('vlmModel').value.trim(),
    vlm_frames:parseInt($('vlmFrames').value||'6',10)
  };
}
function fill(s){
  if(!s)return;
  $('target').value=s.target||''; $('limit').value=s.limit??0;
  $('tabVideos').checked=(s.tabs||'').includes('videos');
  $('tabShorts').checked=(s.tabs||'').includes('shorts');
  $('frames').value=s.frames??0; $('realtime').checked=!!s.realtime;
  $('headed').checked=!!s.headed;
  $('retry').checked=!!s.retry; $('lang').value=s.lang||'tr';
  $('vlmBase').value=s.vlm_base||''; $('vlmKey').value=s.vlm_key||'';
  $('vlmModel').value=s.vlm_model||''; $('vlmFrames').value=s.vlm_frames??6;
}

async function jget(u){const r=await fetch(u);return r.json();}
async function jpost(u,body){
  const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return r.json();}

async function run(action){
  const data=collect();
  data.action=action;
  const res=await jpost('/api/run',data);
  if(!res.ok && res.error) alert(res.error);
  await refresh();
}

function renderState(s){
  busy=s.busy;
  const ph=$('phase');
  ph.textContent=s.phase+(s.busy?' …':'');
  ph.className='phase'+(s.busy?' busy':(s.phase==='hata'?' err':''));
  for(const k of ['listed','watched','analyzed','error'])
    $('c-'+k).textContent=(s.counts&&s.counts[k])||0;
  $('reportLink').style.display=s.report?'':'none';
  $('openReport').disabled=!s.report;
  document.querySelectorAll('button[data-run]').forEach(b=>b.disabled=busy);
  const log=$('log');
  const txt=(s.lines||[]).join('\\n');
  if(txt){log.style.display='block';
    if(log.textContent!==txt){log.textContent=txt;log.scrollTop=log.scrollHeight;}}
}

function renderRows(rows){
  const tb=$('tbody');
  if(!rows||!rows.length){
    tb.innerHTML='<tr><td colspan="8" class="empty">Henüz kayıt yok — önce <b>Tarama</b> çalıştırın.</td></tr>';
    return;}
  tb.innerHTML=rows.map(v=>{
    const thumb=v.thumb?('<img class="thumb" loading="lazy" src="'+v.thumb+'">')
                       :'<div class="noimg">kare yok</div>';
    const title='<a href="'+esc(v.url||('#')+v.id)+'" target="_blank">'+esc(v.title)+'</a>';
    const type=v.is_short?'<span class="type">SHORTS</span>':'<span class="type">VİDEO</span>';
    const status='<span class="st">'+(ST[v.status]||v.status||'')+
      (v.frames?' · '+v.frames+' kare':'')+
      (v.transcript_lang?' · '+esc(v.transcript_lang):'')+
      (v.sayac?' · 📊 '+esc(v.sayac):'')+
      (v.belge?' · 🔎 '+esc(v.belge):'')+'</span>'+
      (v.error?'<div class="st bad">'+esc(v.error)+'</div>':'');
    const verdict=v.karar?('<span class="verdict '+verdictClass(v.karar)+'">'+esc(v.karar)+'</span>')
                        :'<span class="st">—</span>';
    return '<tr><td>'+thumb+'</td><td class="title">'+title+
      (v.ozet?'<div class="muted">'+esc(v.ozet.slice(0,140))+'</div>':'')+'</td>'+
      '<td>'+type+'</td><td>'+status+'</td>'+
      '<td class="num">'+scoreCell(v.uyum)+'</td>'+
      '<td class="num">'+scoreCell(v.gerceklik)+'</td>'+
      '<td class="num">'+scoreCell(v.genel)+'</td>'+
      '<td>'+verdict+'</td></tr>';
  }).join('');
}

async function refresh(){
  try{
    const [s,rows]=await Promise.all([jget('/api/state'),jget('/api/videos')]);
    renderState(s);
    if(!busy && !filled && s.settings){fill(s.settings); filled=true;}
    renderRows(rows);
    $('rows').classList.toggle('spin',busy);
  }catch(e){/* sunucu kısa süreli meşgul olabilir */}
}

let searchItems=[];
async function doSearch(){
  const q=$('q').value.trim();
  const box=$('results');
  if(!q){box.style.display='none';return;}
  box.style.display='flex';
  box.innerHTML='<span class="muted">aranıyor…</span>';
  try{
    const r=await jget('/api/search?q='+encodeURIComponent(q));
    if(!r.ok){box.innerHTML='<span class="muted">arama hatası: '+esc(r.error||'')+'</span>';return;}
    if(!r.items.length){box.innerHTML='<span class="muted">sonuç yok — @kanal yazıp Tarama da deneyebilirsiniz</span>';return;}
    searchItems=r.items;
    box.innerHTML=r.items.map((c,i)=>'<button class="res" data-i="'+i+'">'+esc(c.title)+
      (c.meta?' <span class="muted">'+esc(c.meta)+'</span>':'')+'</button>').join('');
    box.querySelectorAll('.res').forEach(b=>b.addEventListener('click',()=>{
      const c=searchItems[+b.dataset.i];
      $('target').value=c.url;
      box.style.display='none';
      filled=true;   // seçilen hedef sonraki yenilemede ezilmesin
    }));
  }catch(e){box.innerHTML='<span class="muted">arama hatası</span>';}
}
$('searchBtn').addEventListener('click',doSearch);
$('q').addEventListener('keydown',e=>{if(e.key==='Enter')doSearch();});

document.querySelectorAll('button[data-run]').forEach(b=>{
  b.addEventListener('click',()=>run(b.dataset.run));
});
refresh();
setInterval(refresh,1600);
</script>
</body></html>
"""


def serve(port: int = 8788, open_browser: bool = True) -> int:
    config.ensure_dirs()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        print(f"Port {port} açılamadı: {exc}\nBaşka bir port deneyin: --port 8789")
        return 2
    url = f"http://127.0.0.1:{port}"
    print(f"Panel çalışıyor: {url}")
    print("Durdurmak için Ctrl+C")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — tarayıcı yoksa panel yine çalışır
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nPanel kapatıldı.")
    finally:
        httpd.server_close()
    return 0
