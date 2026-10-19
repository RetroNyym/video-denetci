"""Gerçek Chromium ile videoyu **indirmeden** oynatma ve kare yakalama.

- Playwright gerçek bir Chromium başlatır (BotGuard/TLS parmak izi gibi
  tersine mühendislik engelleri gerçek tarayıcı olduğu için devreye girmez).
- İki mod:
    * örneklem (varsayılan): video player'da belirli anlara atlanır, o anın
      karesi alınır — hızlı, video dosyası hiç inmez.
    * gerçek zamanlı (--realtime): video baştan sona oynar, belirli
      aralıklarla kare alınır.
- Opsiyonel hesap oturumu `login` komutuyla kaydedilen storage_state ile
  kullanılır (yaş sınırlı / abone içeriği için); analiz için zorunlu değildir.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

from . import config

# Kabul diyaloğu (EEA/consent) ve yaş doğrulama butonları — en iyi çaba
_ACCEPT_PATTERNS = [
    r"Tümünü kabul et",
    r"Kabul ediyorum",
    r"Accept all",
    r"I agree",
    r"Yes, I'?m in",
    r"Devam et",
]

_JS_PLAYER_INDEX = """() => {
  const vids = Array.from(document.querySelectorAll('video'));
  const mp = document.querySelector('#movie_player video');
  if (mp) return vids.indexOf(mp);
  let best = -1, area = 0;
  vids.forEach((v, i) => {
    const r = v.getBoundingClientRect();
    const a = Math.max(0, r.width) * Math.max(0, r.height);
    if (a > area) { area = a; best = i; }
  });
  return best;
}"""

_JS_STATE = """v => ({
  t: v.currentTime, d: v.duration, paused: v.paused, ready: v.readyState,
  w: v.videoWidth, h: v.videoHeight, ended: v.ended
})"""


def _accept_if_needed(page) -> None:
    """Consent/yaş diyaloğu görünürse onayla (en iyi çaba)."""
    try:
        has_dialog = (
            page.locator(
                "ytd-consent-baseline-light-box, ytd-consent-baseline-light-box-v2, "
                "tp-yt-paper-dialog, form[action*='consent']"
            ).count()
            > 0
        )
        if not has_dialog and "consent" not in page.url:
            return
    except PWError:
        return
    for pattern in _ACCEPT_PATTERNS:
        try:
            btn = page.get_by_role("button", name=re.compile(pattern, re.I)).first
            btn.wait_for(state="visible", timeout=900)
            btn.click(timeout=2500)
            page.wait_for_timeout(600)
            return
        except (PWTimeout, PWError):
            continue


def _wait_until(page, predicate_js: str, arg, timeout_ms: int, poll_ms: int = 200) -> bool:
    """page.wait_for_function yerine polling.

    YouTube, Trusted Types CSP'si (`require-trusted-types-for 'script'`)
    servis ettiği için wait_for_function'ın string predicate eval'i reddedilir;
    page.evaluate (callFunctionOn) bu kısıttan etkilenmez.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            if page.evaluate(predicate_js, arg):
                return True
        except PWError:
            pass
        page.wait_for_timeout(poll_ms)
    return False


def _find_player(page, timeout_ms: int = config.PLAYER_TIMEOUT_MS):
    """Oynatıcı <video> elementini (index, locator) olarak bulur."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            idx = page.evaluate(_JS_PLAYER_INDEX)
        except PWError:
            idx = -1
        if isinstance(idx, int) and idx >= 0:
            return idx, page.locator("video").nth(idx)
        page.wait_for_timeout(400)
    raise RuntimeError("Oynatıcı bulunamadı (video kaldırılmış, gizli olabilir)")


def _sample_times(duration: float, count: int) -> list[float]:
    """Başlangıç/son güvenli payı ile eşit aralıklı zamanlar."""
    count = max(1, int(count))
    if duration < 6:
        return [max(0.1, duration / 2)] if duration > 0 else [0.5]
    lo = min(2.0, duration * 0.05)
    hi = duration * 0.95
    if count == 1:
        return [(lo + hi) / 2]
    return [lo + (hi - lo) * (i / (count - 1)) for i in range(count)]


def _cleanup_old_frames(folder: Path) -> None:
    kareler = folder / "kareler"
    if kareler.exists():
        for old in kareler.glob("kare_*.jpg"):
            try:
                old.unlink()
            except OSError:
                pass


def watch_video(
    item: dict,
    *,
    headless: bool = True,
    realtime: bool = False,
    frames: int | None = None,
) -> dict:
    """Videoyu oynatır ve kareleri `out/<id>/kareler/` altına yazar.

    Dönen sözlük: frames, duration, watched, mode, resolution, url_used, error
    """
    vid = item["id"]
    folder = config.OUT / vid
    kareler = folder / "kareler"
    kareler.mkdir(parents=True, exist_ok=True)
    _cleanup_old_frames(folder)

    primary = (
        f"https://www.youtube.com/shorts/{vid}" if item.get("is_short") else item["url"]
    )
    fallback = f"https://www.youtube.com/watch?v={vid}"
    if not item.get("is_short"):
        fallback = f"https://www.youtube.com/shorts/{vid}"

    n = frames or (config.FRAMES_SHORT if item.get("is_short") else config.FRAMES_DEFAULT)
    result: dict = {
        "frames": [],
        "duration": 0.0,
        "watched": 0.0,
        "mode": "gerçek zamanlı" if realtime else "örneklem",
        "resolution": "",
        "url_used": primary,
        "error": "",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless, args=["--autoplay-policy=no-user-gesture-required"]
        )
        try:
            ctx_opts: dict = {
                "viewport": {"width": 1280, "height": 800},
                "locale": "tr-TR",
            }
            if config.STORAGE_STATE.exists():
                ctx_opts["storage_state"] = str(config.STORAGE_STATE)
            ctx = browser.new_context(**ctx_opts)
            ctx.set_default_timeout(20000)
            page = ctx.new_page()

            # --- aç: ilk denemede oynatıcı yoksa alternatif URL ile tekrar ---
            idx = -1
            player = None
            last_error = ""
            for url in (primary, fallback):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    result["url_used"] = url
                    _accept_if_needed(page)
                    idx, player = _find_player(page)
                    _accept_if_needed(page)
                    break
                except (RuntimeError, PWTimeout) as exc:
                    last_error = str(exc)
                    player = None
            if player is None:
                raise RuntimeError(last_error or "Oynatıcı açılamadı")

            # --- oynatmayı başlat (sessiz; autoplay politikası serbest) -------
            player.evaluate(
                "v => { v.muted = true; v.autoplay = true;"
                " return v.play().then(() => true).catch(() => false); }"
            )
            page.wait_for_timeout(900)
            state = player.evaluate(_JS_STATE)
            if state.get("paused"):
                # oynat overlay'i/tuşu: kare alanına tıkla ve yeniden dene
                try:
                    player.click(position={"x": 60, "y": 40}, timeout=2000)
                except PWError:
                    pass
                player.evaluate("v => v.play().catch(() => {})")
                page.wait_for_timeout(800)
                state = player.evaluate(_JS_STATE)
            if state.get("paused") and state.get("ready", 0) >= 2:
                result["error"] = "Oynatma başlamadı (sayfa engeli/yaş kısıtı olabilir)"
                return result

            # --- süre ---
            _wait_until(
                page,
                "(i) => { const v = document.querySelectorAll('video')[i];"
                " return !!(v && Number.isFinite(v.duration) && v.duration > 0); }",
                idx,
                timeout_ms=10000,
            )
            state = player.evaluate(_JS_STATE)
            duration = float(state.get("d") or 0)
            result["duration"] = round(duration, 2)
            result["resolution"] = f"{int(state.get('w') or 0)}x{int(state.get('h') or 0)}"
            if duration <= 0:
                raise RuntimeError("Video süresi okunamadı")

            # --- kare yakalama ---
            if realtime:
                targets = _sample_times(duration, n * config.FRAMES_REALTIME_FACTOR)
                player.evaluate(
                    "v => { v.currentTime = 0; v.muted = true;"
                    " return v.play().catch(() => {}); }"
                )
                shot = 0
                deadline = time.time() + duration * 1.2 + 20
                while shot < len(targets) and time.time() < deadline:
                    state = player.evaluate(_JS_STATE)
                    if state.get("ended") or state.get("t", 0) >= duration - 0.35:
                        break
                    if state.get("t", 0) >= targets[shot]:
                        path = kareler / f"kare_{shot + 1:03d}.jpg"
                        player.screenshot(path=str(path), type="jpeg", quality=80)
                        result["frames"].append(str(path))
                        shot += 1
                    page.wait_for_timeout(300)
                result["watched"] = round(float(state.get("t") or 0), 2)
            else:
                for i, t in enumerate(_sample_times(duration, n), 1):
                    t = round(t, 2)
                    player.evaluate(
                        "(v, t) => { v.currentTime = t; v.play().catch(() => {}); }", t
                    )
                    _wait_until(
                        page,
                        "(a) => { const v = document.querySelectorAll('video')[a[0]];"
                        " return !!(v && Math.abs(v.currentTime - a[1]) < 0.6"
                        " && v.readyState >= 2); }",
                        [idx, t],
                        timeout_ms=15000,
                    )
                    page.wait_for_timeout(350)
                    path = kareler / f"kare_{i:03d}.jpg"
                    player.screenshot(path=str(path), type="jpeg", quality=80)
                    result["frames"].append(str(path))
                result["watched"] = round(duration, 2)

            ctx.close()
        finally:
            browser.close()

    if not result["frames"] and not result["error"]:
        result["error"] = "Hiç kare yakalanamadı"
    return result


def login() -> None:
    """Hesap oturumunu bir kez kaydeder (elle giriş, başlıklı pencere)."""
    config.ensure_dirs()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800}, locale="tr-TR"
        )
        page = ctx.new_page()
        page.goto("https://www.youtube.com", wait_until="domcontentloaded", timeout=45000)
        try:
            input(
                "Tarayıcıda YouTube hesabınıza giriş yapın "
                "(bu pencerede, gizli sekme değil). Bitirince Enter'a basın... "
            )
        except EOFError:
            pass
        ctx.storage_state(path=str(config.STORAGE_STATE))
        browser.close()
    print(f"Oturum kaydedildi: {config.STORAGE_STATE}")
