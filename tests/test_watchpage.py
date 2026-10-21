"""watchpage birim testleri — ağ gerektirmeyen kısımlar (sayaç okuma)."""

from __future__ import annotations

import denetci.watchpage as wp


FAKE_HTML = (
    '<html><script>var ytInitialPlayerResponse = '
    '{"videoDetails":{"videoId":"abc","viewCount":"1234567"}};</script>'
    ' ... "viewCountText":{"simpleText":"1,2 mn görüntüleme"}'
)


def test_view_count_tam_sayi_okur(monkeypatch):
    monkeypatch.setattr(wp, "_get", lambda url, params=None: FAKE_HTML)
    assert wp.view_count("abc123def4") == 1234567


def test_view_count_yuvarlanmis_metni_kullanmaz(monkeypatch):
    # HTML'de yalnızca yuvarlanmış metin varsa None (tam sayı yok)
    html = '<div>"viewCountText":{"simpleText":"1,2 mn görüntüleme"}</div>'
    monkeypatch.setattr(wp, "_get", lambda url, params=None: html)
    assert wp.view_count("abc123def4") is None


def test_view_count_ag_hatasinda_none(monkeypatch):
    def boom(url, params=None):
        raise wp.WatchError("HTTP 500")

    monkeypatch.setattr(wp, "_get", boom)
    assert wp.view_count("abc123def4") is None


def test_view_count_izlemeyi_engellemez(monkeypatch):
    # çağrı hiçbir koşulda yükseltmez: doğrudan None ile döner
    monkeypatch.setattr(wp, "_get", lambda url, params=None: "")
    assert wp.view_count("abc123def4") is None
