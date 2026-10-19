"""Panel (GUI) HTTP arayüzü testleri — ağ/tarayıcı gerektirmez."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from denetci import panel


@pytest.fixture(scope="module")
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.read()


def _post(url: str, data: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_index_sayfasi_200(base_url):
    status, body = _get(base_url + "/")
    assert status == 200
    assert b"video-denetci" in body


def test_state_yapisi(base_url):
    status, body = _get(base_url + "/api/state")
    assert status == 200
    state = json.loads(body.decode("utf-8"))
    assert "busy" in state and "phase" in state
    assert isinstance(state["counts"], dict)
    assert "target" in state["settings"]


def test_videos_listesi(base_url):
    status, body = _get(base_url + "/api/videos")
    assert status == 200
    assert isinstance(json.loads(body.decode("utf-8")), list)


def test_ayarlar_kayit_ve_yansima(base_url):
    _post(base_url + "/api/settings", {"target": "@ornek-kanal", "frames": 7})
    _, body = _get(base_url + "/api/state")
    st = json.loads(body.decode("utf-8"))["settings"]
    assert st["target"] == "@ornek-kanal"
    assert st["frames"] == 7
    _post(base_url + "/api/settings", {"target": "", "frames": 0})  # temizle


def test_bos_arama_aga_gitmez(base_url):
    # boş sorgu: sunucu ağ çağırmadan boş liste döndürmeli
    status, body = _get(base_url + "/api/search?q=")
    assert status == 200
    data = json.loads(body.decode("utf-8"))
    assert data == {"ok": True, "items": []}


def test_kok_disi_dosya_403(base_url):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base_url + "/out/..%2Fdata%2Fdenetci.db")
    assert exc.value.code == 403


def test_bulunmayan_dosya_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base_url + "/out/yok-boyle-video/kare_999.jpg")
    assert exc.value.code == 404


def test_gecersiz_action_400(base_url):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(base_url + "/api/run", {"action": "bogus"})
    assert exc.value.code == 400
