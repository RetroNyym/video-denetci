"""video-denetci CLI.

Komutlar:
  python -m denetci login                      # hesap oturumunu bir kez kaydet (ops.)
  python -m denetci panel                      # yerel web paneli (GUI), tarayıcıda
  python -m denetci search "mkbhd"             # YouTube'da kanal ara (ad/URL al)
  python -m denetci scan @kanal                # tüm videolar + Shorts listelenir
  python -m denetci watch                      # listelenenleri oynatıp kare yakalar
  python -m denetci verify                     # izleme belgelerini doğrula (kanıt)
  python -m denetci anchor                     # belgeleri GitHub'a çapala (3. taraf zaman damgası)
  python -m denetci analyze                    # başlık↔içerik + gerçeklik denetimi
  python -m denetci report                     # HTML rapor
  python -m denetci run @kanal                 # hepsi tek komutta
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import analyzer, collector, config, receipt, report, store, watchpage, watcher


# ---------------------------------------------------------------- yardımcılar
def _tabs(value: str) -> tuple[str, ...]:
    tabs = tuple(t.strip() for t in value.split(",") if t.strip() in ("videos", "shorts"))
    return tabs or ("videos", "shorts")


def _vlm_cfg(args: argparse.Namespace) -> dict | None:
    base = args.vlm_base or os.environ.get("VLM_BASE_URL") or ""
    if not base:
        return None
    model = args.vlm_model or os.environ.get("VLM_MODEL") or ""
    if not model and "api.openai.com" in base:
        model = "gpt-4o-mini"
    if not model:
        print("[!] VLM model belirtilmedi (--vlm-model / VLM_MODEL); 'gpt-4o-mini' deneniyor.")
        model = "gpt-4o-mini"
    return {
        "base": base,
        "key": args.vlm_key or os.environ.get("VLM_API_KEY") or "",
        "model": model,
        "max_frames": args.vlm_frames,
    }


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------- komutlar
def cmd_login(_args: argparse.Namespace) -> int:
    watcher.login()
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    target = args.target
    if collector.is_video_target(target):
        items = [collector.video_item(target)]
        channel = ""
        print(f"[1/1] Tek video: {items[0]['id']}")
        # başlık + metinleri hemen çek (tek video hızlı; analiz önbelleği de dolar)
        try:
            data = watchpage.fetch(items[0]["id"], lang=getattr(args, "lang", "tr"))
            if data.get("title"):
                items[0]["title"] = data["title"]
            watchpage.save_text_cache(items[0]["id"], data)
            print(f"  başlık: {(items[0]['title'] or '(başlık yok)')[:70]}")
        except watchpage.WatchError as exc:
            print(f"  (başlık alınamadı: {exc})", file=sys.stderr)
    else:
        print(f"[1/1] Kanal listeleniyor: {target} (sekmeler: {', '.join(_tabs(args.tabs))})")
        try:
            items = collector.collect(target, limit=args.limit, tabs=_tabs(args.tabs))
        except Exception as exc:  # yt-dlp hataları
            print(f"  HATA: {exc}", file=sys.stderr)
            return 2
        channel = target
    if not items:
        print("  UYARI: video bulunamadı (kanal yeni/gizli olabilir).", file=sys.stderr)
        return 3
    store.upsert_videos(items, channel=channel)
    n_short = sum(1 for i in items if i["is_short"])
    print(f"  {len(items)} video listelendi ({n_short} Shorts, {len(items) - n_short} video).")
    print(f"  Durum: {store.counts()}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    pending = store.rows(("listed",))
    if args.retry:
        pending += store.rows("error")
    if not pending:
        print("İzlenecek yeni video yok (durum: listed). Önce `scan` çalıştırın.")
        return 0
    print(f"[watch] {len(pending)} video oynatılacak "
          f"({'gerçek zamanlı' if args.realtime else 'örneklem'} mod, "
          f"headless={'evet' if not args.headed else 'hayır'})")
    ok = err = 0
    for i, row in enumerate(pending, 1):
        tag = "Shorts" if row["is_short"] else "video"
        print(f"  ({i}/{len(pending)}) [{tag}] {(row['title'] or row['id'])[:60]}")
        try:
            res = watcher.watch_video(
                row,
                headless=not args.headed,
                realtime=args.realtime,
                frames=args.frames or None,
            )
        except Exception as exc:
            store.set_fields(row["id"], status="error", error=f"izleme: {exc}")
            print(f"    HATA: {exc}", file=sys.stderr)
            err += 1
            continue
        if res.get("error") and not res.get("frames"):
            store.set_fields(row["id"], status="error", error=res["error"])
            print(f"    HATA: {res['error']}", file=sys.stderr)
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
        print(f"    {len(res['frames'])} kare · {res.get('duration', 0)} sn · {res['mode']}")
        belge = receipt.create(row, res)
        if belge:
            print(f"    izleme belgesi: izleme_belgesi.json · özet {belge['ozet'][:16]}…")
        ok += 1
    print(f"[watch] tamam: {ok} izlendi, {err} hata.")
    return 0 if ok or not err else 4


def cmd_analyze(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    statuses: tuple[str, ...] = ("watched",)
    if args.retry:
        statuses = ("watched", "analyzed", "error")
    pending = store.rows(statuses)
    if not pending:
        print("Denetlenecek video yok (durum: watched). Önce `watch` çalıştırın.")
        return 0
    vlm_cfg = _vlm_cfg(args)
    if vlm_cfg:
        print(f"[analyze] {len(pending)} video · VLM: {vlm_cfg['base']} ({vlm_cfg['model']})")
    else:
        print(f"[analyze] {len(pending)} video · yalnız anahtarsız sezgisel katman "
              "(gerçeklik skoru için --vlm-base ayarlayın)")
    ok = err = 0
    for i, row in enumerate(pending, 1):
        vid = row["id"]
        folder = config.OUT / vid
        print(f"  ({i}/{len(pending)}) {(row['title'] or vid)[:60]}")

        # transkript + açıklama (indirme yok; yt-dlp altyazı akışı, yedek watch sayfası)
        transcript = description = ""
        lang = ""
        tp, dp = folder / "transkript.txt", folder / "aciklama.txt"
        if tp.exists() and tp.stat().st_size > 0 and row.get("title"):
            transcript = tp.read_text(encoding="utf-8", errors="ignore")
            description = dp.read_text(encoding="utf-8", errors="ignore") if dp.exists() else ""
        else:
            # altyazı dilini başlık diline göre seç (uyum aynı dilde ölçünsün);
            # başlık eksikse bu çağrı başlığı da getirir
            desc_hint = ""
            if dp.exists():
                desc_hint = dp.read_text(encoding="utf-8", errors="ignore")[:400]
            prefer = analyzer.detect_lang(row.get("title", ""), desc_hint) or args.lang
            try:
                data = watchpage.fetch(vid, lang=prefer)
                transcript = data["transcript"]
                description = data["description"]
                lang = data["language"]
                if data.get("title") and not row.get("title"):
                    store.set_fields(vid, title=data["title"])
                    row["title"] = data["title"]
                    print(f"    başlık: {data['title'][:60]}")
                watchpage.save_text_cache(vid, data)
            except watchpage.WatchError as exc:
                print(f"    transkript alınamadı: {exc}", file=sys.stderr)

        frames = sorted(str(p) for p in (folder / "kareler").glob("kare_*.jpg"))
        try:
            analysis = analyzer.analyze(
                row, transcript, description, frames, vlm_cfg=vlm_cfg, lang=args.lang
            )
        except Exception as exc:
            store.set_fields(vid, status="error", error=f"analiz: {exc}")
            print(f"    HATA: {exc}", file=sys.stderr)
            err += 1
            continue
        _save_json(folder / "analiz.json", analysis)
        store.set_fields(
            vid,
            status="analyzed",
            analysis=analysis,
            transcript_lang=lang or row.get("transcript_lang", ""),
            error="",
        )
        s = analysis["skorlar"]
        print(f"    karar: {analysis['karar']} · genel: {s['genel']} "
              f"(uyum {s['baslik_icerik']}, gerçeklik {s['gerceklik']})")
        ok += 1
    print(f"[analyze] tamam: {ok} denetlendi, {err} hata.")
    return 0 if ok or not err else 4


def cmd_anchor(args: argparse.Namespace) -> int:
    """Belgeleri depoya çapalar: GitHub commit zamanı üçüncü taraf zaman damgasıdır."""
    config.ensure_dirs()
    rows = store.rows(("watched", "analyzed", "error"))
    if args.video:
        rows = [r for r in rows if r["id"] == args.video]
        if not rows:
            print(f"Kayıt yok: {args.video}")
            return 3
    dest = config.ROOT / receipt.ANCHOR_DIR_NAME
    copied = receipt.stage_anchor(rows, dest)
    if not copied:
        print("Çapalanacak belge yok. Önce `watch` çalıştırın.")
        return 0
    print(f"{len(copied)} belge → {dest}/")
    durum, mesaj = receipt.anchor_to_git(dest, copied)
    if durum == "hata":
        print(f"HATA: {mesaj}", file=sys.stderr)
        return 2
    print(mesaj)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """İzleme belgelerini doğrular (belge özeti + kare dosyalarının SHA-256'ı)."""
    config.ensure_dirs()
    rows = store.rows(("watched", "analyzed", "error"))
    if args.video:
        rows = [r for r in rows if r["id"] == args.video]
        if not rows:
            print(f"Kayıt yok: {args.video}")
            return 3
    if not rows:
        print("Doğrulanacak kayıt yok. Önce `watch` çalıştırın.")
        return 0
    results = receipt.verify_all(rows)
    ok = sum(1 for r in results if r["durum"] == "ok")
    for r in results:
        mark = {"ok": "✓", "bozuk": "✗", "yok": "–"}[r["durum"]]
        print(f"  {mark} {r['video_id']:<14} {r['durum']:<6} {r['not']}")
        if r["belge_ozeti"]:
            print(f"      özet: {r['belge_ozeti'][:32]}… · zaman: {r['zaman'] or '?'}")
    n_bozuk = sum(1 for r in results if r["durum"] == "bozuk")
    n_yok = sum(1 for r in results if r["durum"] == "yok")
    print(f"[verify] {ok} doğrulandı, {n_bozuk} bozuk, {n_yok} belgesiz.")
    return 5 if n_bozuk else 0


def cmd_report(_args: argparse.Namespace) -> int:
    rows = store.rows("analyzed")
    if not rows:
        print("Kayıtlı denetim yok. Önce `analyze` çalıştırın.")
        return 3
    report.console_table(rows)
    path = report.build(rows, channel=_channel_of(rows))
    print(f"\nHTML rapor: {path}")
    print(f"           {config.REPORTS / 'latest.html'}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """YouTube'da kanal araması (aynı arama paneldeki arama çubuğunda da var)."""
    print(f"[search] {args.query!r} için kanallar aranıyor…")
    try:
        items = watchpage.search_channels(args.query, limit=args.limit or 8)
    except watchpage.WatchError as exc:
        print(f"  HATA: {exc}", file=sys.stderr)
        return 2
    if not items:
        print("  Sonuç yok.")
        return 3
    for i, c in enumerate(items, 1):
        meta = f" · {c['meta']}" if c.get("meta") else ""
        print(f"  {i:2}. {c['title'][:44]:<44}{meta}")
        print(f"      {c['url']}")
    print("\nBu URL'yi panele ya da `scan` hedefine yapıştırabilirsiniz.")
    return 0


def cmd_panel(args: argparse.Namespace) -> int:
    """Yerel web panelini (GUI) başlatır — 127.0.0.1:8788."""
    from . import panel

    return panel.serve(port=args.port, open_browser=not args.no_browser)


def _channel_of(rows: list[dict]) -> str:
    for row in rows:
        if row.get("channel"):
            return row["channel"]
    return ""


def cmd_run(args: argparse.Namespace) -> int:
    code = cmd_scan(args)
    if code:
        return code
    if not args.no_watch:
        code = cmd_watch(args)
        if code:
            return code
    else:
        print("[watch] atlandı (--no-watch); analiz yalnız metin + varsa eski karelerle yapılır.")
    code = cmd_analyze(args)
    if code:
        return code
    return cmd_report(args)


# ---------------------------------------------------------------------- parser
def _add_vlm_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--vlm-base", help="OpenAI uyumlu uç nokta (örn. https://api.openai.com/v1)")
    p.add_argument("--vlm-key", help="API anahtarı (VLM_API_KEY ortamıyla aynı)")
    p.add_argument("--vlm-model", help="Model adı (VLM_MODEL ortamıyla aynı)")
    p.add_argument("--vlm-frames", type=int, default=config.VLM_MAX_FRAMES,
                   help=f"VLM'e giden kare sayısı (varsayılan {config.VLM_MAX_FRAMES})")
    p.add_argument("--lang", default="tr", help="transkript tercih edilen dil (varsayılan tr)")


def main(argv: list[str] | None = None) -> int:
    # Windows konsolu (cp1252/cp1254) Türkçe dışı karakterleri basamaz;
    # çıktı akışlarını UTF-8'e çevir (↑ ↔ ✓ gibi semboller için).
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    config.ensure_dirs()
    parser = argparse.ArgumentParser(
        prog="denetci",
        description="YouTube video içerik doğrululuk denetçisi "
                    "(başlık↔içerik uyumu, clickbait, gerçeklik) — video indirmeden.",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_search = sub.add_parser("search", help="YouTube'da kanal ara (URL'yi taramaya yapıştır)")
    p_search.add_argument("query", help="arama metni (örn. mkbhd)")
    p_search.add_argument("--limit", type=int, default=8, help="sonuç sayısı (varsayılan 8)")
    p_search.set_defaults(func=cmd_search)

    p_scan = sub.add_parser("scan", help="kanalın tüm videolarını + Shorts'ları listele")
    p_scan.add_argument("target", help="@kanal, UC... ID, kanal URL ya da video URL")
    p_scan.add_argument("--limit", type=int, default=0, help="sekme başına üst sınır (0 = tümü)")
    p_scan.add_argument("--tabs", default="videos,shorts", help="taranacak sekmeler")
    p_scan.set_defaults(func=cmd_scan)

    p_watch = sub.add_parser("watch", help="listelenen videoları oynatıp kare yakala")
    p_watch.add_argument("--realtime", action="store_true",
                         help="videoyu baştan sona oynat (yavaş); varsayılan: örneklem")
    p_watch.add_argument("--headed", action="store_true", help="tarayıcıyı göster")
    p_watch.add_argument("--frames", type=int, default=0, help="video başına kare (0 = varsayılan)")
    p_watch.add_argument("--retry", action="store_true", help="hatalıları da yeniden dene")
    p_watch.set_defaults(func=cmd_watch)

    p_an = sub.add_parser("analyze", help="başlık↔içerik + gerçeklik denetimi")
    p_an.add_argument("--retry", action="store_true",
                      help="daha önce denetlenmiş/hatalı videoları da yeniden denetle")
    _add_vlm_args(p_an)
    p_an.set_defaults(func=cmd_analyze)

    p_anchor = sub.add_parser(
        "anchor", help="belgeleri GitHub'a çapala (üçüncü taraf zaman damgası)"
    )
    p_anchor.add_argument("--video", help="yalnızca tek video belgesini çapala")
    p_anchor.set_defaults(func=cmd_anchor)

    p_ver = sub.add_parser("verify", help="izleme belgelerini doğrula (kanıt bütünlüğü)")
    p_ver.add_argument("--video", help="yalnızca tek video kimliğini doğrula")
    p_ver.set_defaults(func=cmd_verify)

    p_rep = sub.add_parser("report", help="HTML rapor üret")
    p_rep.set_defaults(func=cmd_report)

    p_run = sub.add_parser("run", help="scan -> watch -> analyze -> report")
    p_run.add_argument("target", help="@kanal, UC... ID, kanal URL ya da video URL")
    p_run.add_argument("--limit", type=int, default=0, help="sekme başına üst sınır (0 = tümü)")
    p_run.add_argument("--tabs", default="videos,shorts", help="taranacak sekmeler")
    p_run.add_argument("--realtime", action="store_true", help="gerçek zamanlı oynatma")
    p_run.add_argument("--headed", action="store_true", help="tarayıcıyı göster")
    p_run.add_argument("--frames", type=int, default=0, help="video başına kare")
    p_run.add_argument("--retry", action="store_true", help="hatalıları da yeniden dene")
    p_run.add_argument("--no-watch", action="store_true",
                       help="izleme/kare adımını atla (yalnız metin analizi)")
    _add_vlm_args(p_run)
    p_run.set_defaults(func=cmd_run)

    p_login = sub.add_parser("login", help="hesap oturumunu kaydet (opsiyonel, elle giriş)")
    p_login.set_defaults(func=cmd_login)

    p_panel = sub.add_parser("panel", help="yerel web panelini (GUI) aç — tarayıcıda arayüz")
    p_panel.add_argument("--port", type=int, default=8788, help="port (varsayılan 8788)")
    p_panel.add_argument("--no-browser", action="store_true", help="tarayıcıyı otomatik açma")
    p_panel.set_defaults(func=cmd_panel)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    if not hasattr(args, "frames"):
        args.frames = 0
    if not hasattr(args, "realtime"):
        args.realtime = False
    if not hasattr(args, "headed"):
        args.headed = False
    if not hasattr(args, "retry"):
        args.retry = False
    if not hasattr(args, "tabs"):
        args.tabs = "videos,shorts"
    if not hasattr(args, "limit"):
        args.limit = 0
    if not hasattr(args, "no_watch"):
        args.no_watch = False
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
