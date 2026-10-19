"""Konsol tablosu + tek dosya HTML denetim raporu."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from . import config

_SEVERITY = {"güvensiz": 0, "uyumsuz": 0, "şüpheli": 1, "uyumlu": 2, "güvenilir": 2}


def _sort_key(row: dict) -> tuple:
    a = row.get("analysis") or {}
    genel = (a.get("skorlar") or {}).get("genel")
    karar = a.get("karar", "")
    sev = min([s for k, s in _SEVERITY.items() if karar.startswith(k)] or [3])
    return (sev, genel if genel is not None else 999)


def _score_cell(value) -> str:
    if value is None:
        return "<span class='na'>—</span>"
    cls = "ok" if value >= 70 else ("mid" if value >= 45 else "bad")
    return f"<span class='score {cls}'>{value}</span>"


def console_table(rows: list[dict]) -> None:
    rows = [r for r in rows if r.get("analysis")]
    if not rows:
        print("Denetlenmiş kayıt yok. Önce: run / scan -> watch -> analyze")
        return
    rows.sort(key=_sort_key)
    print(f"{'GENEL':>5} {'UYUM':>5} {'GER.':>5}  {'TİP':<7} {'KARAR':<42} BAŞLIK")
    print("-" * 110)
    for row in rows:
        a = row["analysis"]
        s = a.get("skorlar") or {}
        fmt = lambda v: "—" if v is None else str(v)
        title = (a.get("title") or row.get("title") or "")[:44]
        print(
            f"{fmt(s.get('genel')):>5} {fmt(s.get('baslik_icerik')):>5} "
            f"{fmt(s.get('gerceklik')):>5}  "
            f"{'Shorts' if a.get('is_short') else 'video':<7} "
            f"{a.get('karar',''):<42} {title}"
        )


def build(rows: list[dict], channel: str = "") -> Path:
    """HTML raporu üretir; dosya yolunu döndürür."""
    config.ensure_dirs()
    rows = [r for r in rows if r.get("analysis")]
    rows.sort(key=_sort_key)

    counts: dict[str, int] = {}
    for row in rows:
        k = (row["analysis"].get("karar") or "?").split(" (")[0]
        counts[k] = counts.get(k, 0) + 1
    shorts = sum(1 for r in rows if r["analysis"].get("is_short"))

    cards: list[str] = []
    for row in rows:
        a = row["analysis"]
        s = a.get("skorlar") or {}
        heu = a.get("heuristik") or {}
        vlm = a.get("vlm") or {}
        vid = row["id"]
        thumb = ""
        frame_dir = config.OUT / vid / "kareler"
        first = sorted(frame_dir.glob("kare_*.jpg")) if frame_dir.exists() else []
        if first:
            rel = f"../out/{vid}/kareler/{first[0].name}"
            thumb = (
                f"<a href='{rel}' target='_blank'>"
                f"<img src='{rel}' alt='kare'></a>"
            )

        badges = "".join(
            f"<span class='badge'>{html.escape(w)}</span>"
            for w in (heu.get("clickbait") or {}).get("bulunan", [])[:6]
        )
        sinyaller = (vlm.get("gerceklik") or {}).get("sinyaller", []) or []
        sinyaller_html = (
            "<ul>" + "".join(f"<li>{html.escape(str(x))}</li>" for x in sinyaller[:8]) + "</ul>"
            if sinyaller
            else "<span class='na'>VLM sinyali yok</span>"
        )
        iddialar = heu.get("iddialar") or []
        iddialar_html = (
            "<div class='claims'>"
            + ", ".join(
                f"<span class='claim {'ok' if i['icerikte'] else 'bad'}'>"
                f"{html.escape(str(i['iddia']))} {'✓' if i['icerikte'] else '✗'}</span>"
                for i in iddialar[:10]
            )
            + "</div>"
            if iddialar
            else ""
        )
        ozet = a.get("ozet") or heu.get("uyum_not") or ""
        vlm_hata = a.get("vlm_hata") or ""

        cards.append(f"""
    <div class="card">
      <div class="thumb">{thumb or '<div class="noimg">kare yok</div>'}</div>
      <div class="body">
        <div class="head">
          <span class="type">{'SHORTS' if a.get('is_short') else 'VİDEO'}</span>
          <h3>{html.escape(a.get('title') or row.get('title') or vid)}</h3>
          <span class="verdict v{_verdict_class(a.get('karar',''))}">
            {html.escape(a.get('karar',''))}</span>
        </div>
        <div class="scores">
          <div><b>{_txt(s.get('genel'))}</b><span>genel</span></div>
          <div><b>{_txt(s.get('baslik_icerik'))}</b><span>başlık↔içerik</span></div>
          <div><b>{_txt(s.get('gerceklik'))}</b><span>gerçeklik</span></div>
          <div><b>{heu.get('iddia_kanit_orani') if heu.get('iddia_kanit_orani') is not None else '—'}</b><span>iddia kanıtı</span></div>
        </div>
        <p class="ozet">{html.escape(ozet)}</p>
        {f'<p class="badges">{badges}</p>' if badges else ''}
        {iddialar_html}
        <details><summary>Görsel gerçeklik sinyalleri</summary>{sinyaller_html}</details>
        <div class="meta">kare: {a.get('kare_sayisi',0)} ·
          transkript: {'var' if heu.get('transkript_var') else 'yok'}
          {' · ' + html.escape(vlm_hata) if vlm_hata else ''} ·
          <code>{vid}</code></div>
      </div>
    </div>""")

    stamps = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "kayıt yok"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    page = f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Video Denetim Raporu — {html.escape(channel or 'tüm kanallar')}</title>
<style>
  :root {{ --ok:#1a9850; --mid:#f0a202; --bad:#d73027; --ink:#1c2430; --mut:#67717e; }}
  * {{ box-sizing:border-box; }}
  body {{ font:15px/1.5 system-ui,Segoe UI,sans-serif; margin:0; background:#f4f6f9; color:var(--ink); }}
  header {{ background:#0f172a; color:#fff; padding:22px 28px; }}
  header h1 {{ margin:0 0 6px; font-size:20px; }}
  header .sub {{ color:#9fb0c8; font-size:13px; }}
  main {{ max-width:1080px; margin:0 auto; padding:20px; }}
  .legend {{ display:flex; gap:14px; flex-wrap:wrap; margin:14px 0 20px; font-size:13px; color:var(--mut); }}
  .card {{ display:flex; gap:16px; background:#fff; border:1px solid #e3e8ef; border-radius:12px;
           padding:16px; margin-bottom:14px; }}
  .thumb {{ flex:0 0 168px; }}
  .thumb img {{ width:100%; border-radius:8px; display:block; }}
  .noimg {{ width:168px; height:94px; display:flex; align-items:center; justify-content:center;
            background:#eef1f6; color:var(--mut); border-radius:8px; font-size:13px; }}
  .body {{ flex:1; min-width:0; }}
  .head {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
  .head h3 {{ margin:0; font-size:16px; flex:1; min-width:200px; }}
  .type {{ font-size:11px; font-weight:700; letter-spacing:.06em; background:#eef1f6;
           padding:3px 7px; border-radius:5px; color:var(--mut); }}
  .verdict {{ font-size:12px; font-weight:700; padding:4px 9px; border-radius:999px; }}
  .v0 {{ background:#fdecea; color:var(--bad); }}
  .v1 {{ background:#fff6e0; color:#b47700; }}
  .v2 {{ background:#e8f6ee; color:var(--ok); }}
  .v3 {{ background:#eef1f6; color:var(--mut); }}
  .scores {{ display:flex; gap:22px; margin:12px 0 6px; }}
  .scores b {{ font-size:22px; display:block; }}
  .scores span {{ font-size:11px; color:var(--mut); text-transform:uppercase; letter-spacing:.04em; }}
  .ozet {{ margin:8px 0; }}
  .badges {{ margin:6px 0; }}
  .badge {{ display:inline-block; background:#fdecea; color:var(--bad); font-size:12px;
            padding:2px 8px; border-radius:999px; margin-right:6px; }}
  .claims {{ margin:6px 0; font-size:13px; }}
  .claim {{ display:inline-block; margin-right:8px; }}
  .claim.ok {{ color:var(--ok); }} .claim.bad {{ color:var(--bad); }}
  .score.ok {{ color:var(--ok); }} .score.mid {{ color:#b47700; }} .score.bad {{ color:var(--bad); }}
  .na {{ color:var(--mut); }}
  details {{ margin:8px 0; font-size:14px; }}
  details ul {{ margin:6px 0; padding-left:20px; }}
  .meta {{ color:var(--mut); font-size:12px; margin-top:8px; }}
  code {{ background:#f0f3f7; padding:1px 5px; border-radius:4px; }}
</style></head>
<body>
<header>
  <h1>Video Denetim Raporu — {html.escape(channel or 'tüm kanallar')}</h1>
  <div class="sub">{len(rows)} video ({shorts} Shorts) · {stamps} · {now}</div>
  <div class="sub">Başlık↔içerik uyumu anahtarsız sezgisel; gerçeklik skoru
    VLM ile görsel denetimden gelir (yoksa hücre “—” gösterir).</div>
</header>
<main>
  <div class="legend">
    <span> genel ≥ 70 güvenilir</span><span>45–69 şüpheli</span>
    <span>&lt; 45 güvensiz/uyumsuz</span>
    <span>iddia kanıtı: başlıktaki sayılar transkriptte geçiyor mu (✓/✗)</span>
  </div>
  {''.join(cards) if cards else '<p>Denetlenmiş kayıt yok.</p>'}
</main>
</body></html>"""

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.REPORTS / f"denetim_{stamp}.html"
    path.write_text(page, encoding="utf-8")
    latest = config.REPORTS / "latest.html"
    latest.write_text(page, encoding="utf-8")
    return path


def _txt(value) -> str:
    return "—" if value is None else str(value)


def _verdict_class(karar: str) -> str:
    k = karar.lower()
    if k.startswith(("güvensiz", "uyumsuz")):
        return "0"
    if k.startswith("şüpheli"):
        return "1"
    if k.startswith(("güvenilir", "uyumlu")):
        return "2"
    return "3"
