"""Sezgisel analiz katmanı offline testleri (ağ gerektirmez)."""

from denetci import analyzer


def test_clickbait_baslikta_yakalanir():
    r = analyzer.heuristic(
        {"title": "Inanilmaz! Herkesin bilmedigi 3 yontem %100 kanitlandi"},
        "sadece hava anlatti",
        "",
    )
    assert r["clickbait"]["var"] is True
    assert "inanılmaz" in r["clickbait"]["bulunan"]
    assert "%100" in r["clickbait"]["bulunan"]


def test_temiz_baslikta_clickbait_yok():
    # "sirаlari" icindeki 'asla' kelime ortasi: yakalanmamali
    r = analyzer.heuristic({"title": "Kahve demleme siralari: 3 temel yontem"}, "", "")
    assert r["clickbait"]["var"] is False


def test_sayisal_iddia_turkce_metinde_bulunur():
    r = analyzer.heuristic(
        {"title": "Kahve demleme sıraları: 3 temel yöntem"},
        "Üç temel yöntem var: dökme, basınç, french press.",
        "",
    )
    assert r["iddialar"], "iddia listesi bos kalmamali"
    assert all(i["icerikte"] for i in r["iddialar"]), "uc/3 eslesmeli"


def test_uyum_skorlari():
    uzun_iyi = (
        "Kahve demleme yöntemleri neler? Bugün size kahve demleme yöntemleri "
        "hakkında üç farklı yaklaşım anlatacağım. Kahve demleme konusunda en "
        "önemli adım doğru çekirdeği seçmektir. Kavrulma derecesi ve taze "
        "öğütme kahve demleme sonucunu doğrudan etkiler. Öte yandan su "
        "sıcaklığı da kahve demleme kalitesinde belirleyicidir. Demleme "
        "süresini kahve demleme tekniğinize göre ayarlayın."
    )
    uzun_yanlis = (
        "Bugün hava durumundan bahsedeceğiz. Yağmur cuma günü beklentisi var, "
        "sıcaklıklar hafta sonu düşecek. Rüzgar kuzeyden esecek. Deniz "
        "suyu sıcaklığı mevsim normallerinde seyredecek. Meteoroloji yeni "
        "bir açıklama yayınladı. Tatil planı yapanlar için hava koşulları "
        "önemli olacak."
    )
    temiz = analyzer.heuristic({"title": "Kahve demleme yöntemleri"}, uzun_iyi, "")
    uyumsuz = analyzer.heuristic({"title": "Kahve demleme yöntemleri"}, uzun_yanlis, "")
    assert temiz["uyum_skor"] is not None and temiz["uyum_skor"] >= 60
    assert uyumsuz["uyum_skor"] is not None and uyumsuz["uyum_skor"] < 40


def test_kisa_transkriptte_uyum_olcumez():
    # muzik/aksiyon short'larinda anlatim yoktur: 0 yerine None + not
    r = analyzer.heuristic(
        {"title": "Last To Fall Wins"}, "3 2 1 [screaming] pull! go go go yes bye bye", ""
    )
    assert r["uyum_skor"] is None
    assert "yetersiz" in r["uyum_not"]


def test_analyze_vlmsiz_calisir():
    out = analyzer.analyze(
        {"id": "x", "title": "3 gizli yöntem kanıtlandı", "is_short": True},
        "alakasiz icerik",
        "",
        [],
        vlm_cfg=None,
    )
    assert out["vlm"] is None
    assert out["skorlar"]["gerceklik"] is None
    assert out["karar"], "karar bos kalmamali"
    assert out["heuristik"]["clickbait"]["var"] is True


def test_karar_esigi():
    assert analyzer._decision(80, 80)[0] == "güvenilir"
    assert analyzer._decision(50, 50)[0] == "şüpheli"
    assert analyzer._decision(10, 10)[0].startswith("güvensiz")
    assert analyzer._decision(None, None)[0] == "değerlendirilmedi"
