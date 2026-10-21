"""İzleme belgesi (receipt) birim testleri — ağ/tarayıcı gerektirmez."""

from __future__ import annotations

import json

from denetci import config, receipt


def _make_video(tmp_path, monkeypatch, vid: str = "testvid123", frames: int = 2):
    monkeypatch.setattr(config, "OUT", tmp_path)
    folder = tmp_path / vid
    kareler = folder / "kareler"
    kareler.mkdir(parents=True)
    for i in range(1, frames + 1):
        (kareler / f"kare_{i:03d}.jpg").write_bytes(b"\xff\xd8\xff\xe0FAKEJPEG" + bytes([i]))
    (folder / "transkript.txt").write_text("merhaba dünya, bu bir deneme", encoding="utf-8")
    row = {
        "id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": "Deneme Videosu",
        "channel": "@ornek",
        "is_short": False,
    }
    res = {
        "mode": "örneklem",
        "watched": 42.0,
        "duration": 100.0,
        "resolution": "1280x720",
        "url_used": row["url"],
    }
    return row, res, folder


def test_belge_olustur_ve_dogrula(tmp_path, monkeypatch):
    row, res, folder = _make_video(tmp_path, monkeypatch)
    out = receipt.create(row, res)
    assert out is not None and len(out["ozet"]) == 64
    doc = json.loads((folder / receipt.RECEIPT_NAME).read_text(encoding="utf-8"))
    assert doc["izleme"]["oynanan_saniye"] == 42.0
    assert doc["izleme"]["video_saniye"] == 100.0
    assert len(doc["kareler"]) == 2
    assert doc["transkript"]["var"] is True
    r = receipt.verify(row)
    assert r["durum"] == "ok"
    assert "2 kare doğrulandı" in r["not"]


def test_belge_yok(tmp_path, monkeypatch):
    row, _res, _folder = _make_video(tmp_path, monkeypatch)
    r = receipt.verify(row)
    assert r["durum"] == "yok"


def test_kare_degistirince_bozuk(tmp_path, monkeypatch):
    row, res, folder = _make_video(tmp_path, monkeypatch)
    receipt.create(row, res)
    (folder / "kareler" / "kare_001.jpg").write_bytes(b"DEGISTIRILMIK")
    r = receipt.verify(row)
    assert r["durum"] == "bozuk"
    assert "kare_001" in r["not"]


def test_belge_icerigi_degistirince_bozuk(tmp_path, monkeypatch):
    row, res, folder = _make_video(tmp_path, monkeypatch)
    receipt.create(row, res)
    path = folder / receipt.RECEIPT_NAME
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["izleme"]["oynanan_saniye"] = 0.1  # süreyi sahte göster
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    r = receipt.verify(row)
    assert r["durum"] == "bozuk"


def test_kare_silince_bozuk(tmp_path, monkeypatch):
    row, res, folder = _make_video(tmp_path, monkeypatch)
    receipt.create(row, res)
    (folder / "kareler" / "kare_002.jpg").unlink()
    r = receipt.verify(row)
    assert r["durum"] == "bozuk"
    assert "eksik" in r["not"]
