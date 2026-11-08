import pytest

from kitelefon.agents import news_fr


@pytest.fixture(autouse=True)
def frischer_vorrat():
    news_fr.vorrat_leeren()
    yield
    news_fr.vorrat_leeren()


FEED = """<?xml version="1.0"?><rss><channel>
<item><title>Un</title><description>Premier teaser</description></item>
<item><title>Deux</title><description>Deuxième</description></item>
<item><title>Trois</title><description>Troisième</description></item>
<item><title>Quatre</title><description>Vierter faellt weg</description></item>
</channel></rss>"""


@pytest.mark.asyncio
async def test_liefert_hoechstens_drei_eintraege(monkeypatch):
    async def fake_fetch():
        return FEED

    monkeypatch.setattr(news_fr, "_fetch", fake_fetch)
    out = await news_fr.get_news_fr()
    assert len(out["nachrichten"]) == 3
    assert out["nachrichten"][0] == {"titel": "Un", "teaser": "Premier teaser"}


@pytest.mark.asyncio
async def test_netzfehler_wird_zur_sauberen_meldung(monkeypatch):
    async def kaputt():
        raise OSError("offline")

    monkeypatch.setattr(news_fr, "_fetch", kaputt)
    out = await news_fr.get_news_fr()
    assert "fehler" in out


def test_feed_url_ist_die_umgezogene_adresse():
    """RFI hat den Feed umgezogen — die alte www.rfi.fr-Adresse ist tot.

    Am 19. und 22.08. schlug get_news_fr mit 403 fehl; die Pruefung vom
    22.08. zeigte: die alte URL liefert selbst mit Browser-Kennung nur
    eine 404-Seite. Die aktuelle Adresse stammt aus dem
    Apple-Podcasts-Eintrag der Sendung.
    """
    assert "apis.fle.rfi.fr" in news_fr.FEED_URL
    assert "www.rfi.fr" not in news_fr.FEED_URL


def test_anfrage_traegt_eigenen_user_agent(monkeypatch):
    """Ohne eigenen User-Agent sperrt RFI mit 403 aus (Standard von urllib
    steht auf der Sperrliste) — die Anfrage muss den eigenen Namen tragen.
    """
    gesehen = {}

    class FakeAntwort:
        def read(self):
            return FEED.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(anfrage, timeout=None):
        gesehen["ua"] = anfrage.get_header("User-agent")
        gesehen["url"] = anfrage.full_url
        return FakeAntwort()

    monkeypatch.setattr(news_fr.urllib.request, "urlopen", fake_urlopen)
    import asyncio

    asyncio.run(news_fr._fetch())

    assert gesehen["ua"], "kein User-Agent gesetzt — RFI antwortet dann mit 403"
    assert "urllib" not in gesehen["ua"].lower()
    assert gesehen["url"] == news_fr.FEED_URL


@pytest.mark.asyncio
async def test_zweiter_abruf_kommt_aus_dem_vorrat(monkeypatch):
    """Die T3-Brief-Luecke aus Plan 4: Der News-Vorrat hatte keinen
    eigenen Test — Hit, Ablauf und Fehlerfall waren nur fuers Wetter
    gepinnt (Issue #30)."""
    zaehler = {"n": 0}

    async def fake_fetch():
        zaehler["n"] += 1
        return FEED

    monkeypatch.setattr(news_fr, "_fetch", fake_fetch)
    erste = await news_fr.get_news_fr()
    zweite = await news_fr.get_news_fr()
    assert zweite == erste
    assert zaehler["n"] == 1  # kein zweiter Netzabruf


@pytest.mark.asyncio
async def test_abgelaufener_vorrat_holt_frisch(monkeypatch):
    zaehler = {"n": 0}

    async def fake_fetch():
        zaehler["n"] += 1
        return FEED

    monkeypatch.setattr(news_fr, "_fetch", fake_fetch)
    await news_fr.get_news_fr()
    news_fr.vorrat_altern(sekunden=news_fr.NEWS_VORRAT_S + 1)
    await news_fr.get_news_fr()
    assert zaehler["n"] == 2


@pytest.mark.asyncio
async def test_fehler_werden_nicht_gecacht(monkeypatch):
    async def kaputt():
        raise OSError("keine Verbindung")

    monkeypatch.setattr(news_fr, "_fetch", kaputt)
    fehler = await news_fr.get_news_fr()
    assert "fehler" in fehler

    zaehler = {"n": 0}

    async def fake_fetch():
        zaehler["n"] += 1
        return FEED

    monkeypatch.setattr(news_fr, "_fetch", fake_fetch)
    gut = await news_fr.get_news_fr()
    assert "nachrichten" in gut
    assert zaehler["n"] == 1  # der Fehler lag NICHT im Vorrat


@pytest.mark.asyncio
async def test_dtd_wird_abgewiesen_billion_laughs(monkeypatch):
    """Sicherheits-Review: xml.etree.ElementTree.fromstring() verarbeitet
    interne <!ENTITY>-Deklarationen mit — ein boesartiger/gekaperter Feed
    koennte per "billion laughs" den Prozessspeicher sprengen, ohne dass
    eine Exception das faengt. RSS braucht kein DOCTYPE, also muss jedes
    abgelehnt werden (landet als sauberer Fehler, nicht im Vorrat)."""
    bombe = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        "<rss><channel><item><title>&lol2;</title>"
        "<description>d</description></item></channel></rss>"
    )

    async def fake_fetch():
        return bombe

    monkeypatch.setattr(news_fr, "_fetch", fake_fetch)
    out = await news_fr.get_news_fr()
    assert "fehler" in out
    assert news_fr._vorrat is None  # ein abgewiesener Feed landet nicht im Vorrat


@pytest.mark.asyncio
async def test_mutierender_aufrufer_vergiftet_den_vorrat_nicht(monkeypatch):
    """Kopie statt Alias (Finalreview Plan 4): Wer das Ergebnis anfasst,
    darf nicht den Vorrat fuer die naechsten zwei Stunden umschreiben."""

    async def fake_fetch():
        return FEED

    monkeypatch.setattr(news_fr, "_fetch", fake_fetch)
    erste = await news_fr.get_news_fr()
    erste["nachrichten"][0]["titel"] = "VERGIFTET"
    erste["nachrichten"].clear()

    zweite = await news_fr.get_news_fr()  # aus dem Vorrat
    assert len(zweite["nachrichten"]) == 3
    assert zweite["nachrichten"][0]["titel"] == "Un"
