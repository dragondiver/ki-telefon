"""Gesprochene Ansagen bei Stoerungen — Laden und Ausspielen.

Zwei Stellen sagen Paula, dass etwas kaputt ist, statt sie ins Leere
sprechen zu lassen: der Sprachkurs-Ausfall (Spec sec. 7.6, #19) und das
endgueltig tote Mikrofon (#20). Beide laden ihr Asset ueber
``kitelefon.ansagen``.

Die Tests laufen ohne die echten Audio-Assets — sie legen sich eigene
Bytes an. Was hier geprueft wird, ist die Mechanik: dass geladen wird,
dass ein fehlendes Asset den Anruf nicht mitreisst, und vor allem *wann*
die Ansage kommt.
"""

import asyncio

import pytest
from test_mikro_wachhund import FakeAudio

from kitelefon import ansagen
from kitelefon.call_manager import CallManager


@pytest.fixture(autouse=True)
def cache_leeren():
    """``lade`` cacht — sonst leckt ein Test in den naechsten."""
    ansagen.lade.cache_clear()
    yield
    ansagen.lade.cache_clear()


#: Die Namen, die der Code kennt — muessen mit ``ansagen.TEXTE`` uebereinstimmen.
TEXTE_NAMEN = (ansagen.KURS_NICHT_ERREICHBAR, ansagen.MIKRO_TOT)


@pytest.fixture
def ansagen_dir(tmp_path, monkeypatch):
    """Biegt das Asset-Verzeichnis auf ein leeres tmp_path um."""
    monkeypatch.setattr(ansagen, "ANSAGEN_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def asset(ansagen_dir):
    """Legt ein Ansage-Asset im umgebogenen Verzeichnis an."""

    def anlegen(name: str, daten: bytes = b"\x01\x02" * 100) -> bytes:
        (ansagen_dir / f"{name}.pcm").write_bytes(daten)
        return daten

    return anlegen


def test_lade_liefert_die_bytes_des_assets(asset):
    daten = asset(ansagen.MIKRO_TOT)
    assert ansagen.lade(ansagen.MIKRO_TOT) == daten


def test_fehlendes_asset_liefert_leere_bytes_statt_zu_werfen(ansagen_dir, caplog):
    # Verzeichnis existiert, ist aber leer.
    with caplog.at_level("ERROR"):
        assert ansagen.lade(ansagen.KURS_NICHT_ERREICHBAR) == b""
    assert "fehlt" in caplog.text


@pytest.mark.parametrize("name", TEXTE_NAMEN)
def test_ausgeliefertes_asset_ist_da_und_plausibel(name):
    """Ohne monkeypatch: das echte Asset aus dem Repo.

    Faengt den Fall, dass eine .pcm-Datei beim Commit vergessen wurde
    oder ein Merge sie zerlegt hat. Ohne diesen Test faellt das erst im
    Stoerungsfall auf — also genau dann, wenn es niemand gebrauchen kann.
    """
    daten = ansagen.lade(name)
    assert daten, f"{name}.pcm fehlt — mit scripts/ansagen_erzeugen.py erzeugen"
    assert len(daten) % 2 == 0, "PCM16 hat eine gerade Anzahl Bytes"
    sekunden = len(daten) / 2 / 24000
    assert 1.0 < sekunden < 20.0, f"{sekunden:.1f} s ist kein gesprochener Satz"


def test_jeder_wortlaut_hat_einen_namen():
    # Das Erzeugungsskript laeuft ueber TEXTE; ein Name ohne Wortlaut
    # produziert stillschweigend kein Asset.
    assert set(TEXTE_NAMEN) == set(ansagen.TEXTE)


@pytest.mark.asyncio
async def test_wachhund_sagt_erst_an_wenn_er_aufgegeben_hat(asset):
    """Die Ansage darf nicht direkt nach dem letzten Neustart kommen.

    In dem Moment weiss noch niemand, ob der Neustart greift — eine
    Ansage auf einem wieder lebenden Mikrofon waere schlimmer als keine.
    """
    daten = asset(ansagen.MIKRO_TOT)
    audio = FakeAudio()

    def restart_ohne_wirkung():
        audio.restarts += 1  # Stream bleibt tot

    audio.restart_input = restart_ohne_wirkung
    hangup = asyncio.Event()
    task = asyncio.create_task(
        CallManager._mikro_wachhund_lauf(audio, hangup, pruef_s=0.01, tot_s=0.05)
    )
    audio.alter = 999.0
    await asyncio.sleep(0.2)
    hangup.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert audio.gespielt == [daten]  # genau einmal, nicht je Zyklus
    assert audio.restarts == 2  # erst nach dem Budget


@pytest.mark.asyncio
async def test_wachhund_schweigt_wenn_der_neustart_greift(asset):
    asset(ansagen.MIKRO_TOT)
    audio = FakeAudio()  # restart_input setzt alter=0
    hangup = asyncio.Event()
    task = asyncio.create_task(
        CallManager._mikro_wachhund_lauf(audio, hangup, pruef_s=0.01, tot_s=0.05)
    )
    audio.alter = 0.3
    await asyncio.sleep(0.1)
    hangup.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert audio.restarts == 1
    assert audio.gespielt == []  # geheilt — kein Grund fuer eine Ansage


@pytest.mark.asyncio
async def test_wachhund_ueberlebt_ein_fehlendes_asset(ansagen_dir):
    """Ohne Asset bleibt es still, aber der Task laeuft weiter.

    ``ansagen_dir`` ist hier nicht schmueckendes Beiwerk: ohne das
    umgebogene Verzeichnis laedt der Test das echte Asset aus dem Repo
    und prueft das Gegenteil von dem, was sein Name behauptet.
    """
    audio = FakeAudio()

    def restart_ohne_wirkung():
        audio.restarts += 1

    audio.restart_input = restart_ohne_wirkung
    hangup = asyncio.Event()
    task = asyncio.create_task(
        CallManager._mikro_wachhund_lauf(audio, hangup, pruef_s=0.01, tot_s=0.05)
    )
    audio.alter = 999.0
    await asyncio.sleep(0.15)
    hangup.set()
    await asyncio.wait_for(task, timeout=1.0)  # kein Absturz

    assert audio.gespielt == []
