"""Gedaechtnis pro Rolle.

Vorgabe von Florian (2026-08-03): Rollen teilen ihr Wissen NICHT, Hein
merkt sich ein paar Vorlieben und Personendaten, vom Verlauf nur das
letzte Gespraech und hoechstens zwei Wochen lang.
"""

from datetime import UTC, datetime, timedelta

import pytest

from kitelefon.memory import MAX_FAKTEN, RoleMemory
from kitelefon.roles import ROLES, full_instructions

JETZT = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)


@pytest.fixture
def mem(tmp_path):
    return RoleMemory(tmp_path / "tasks.db")


def test_leeres_gedaechtnis_liefert_keinen_prompt_block(mem):
    assert mem.prompt_block("Hamburger", JETZT) == ""


def test_fakten_und_letztes_gespraech_landen_im_block(mem):
    mem.save(
        "Hamburger",
        fakten=["Mag keinen Koriander."],
        zusammenfassung="Ging um Wetter und Musik.",
        jetzt=JETZT,
    )

    block = mem.prompt_block("Hamburger", JETZT + timedelta(days=1))

    assert "Mag keinen Koriander." in block
    assert "Beim letzten Mal: Ging um Wetter und Musik." in block
    assert "ZÄHLE ES NIE AUF" in block


def test_rollen_teilen_ihr_wissen_nicht(mem):
    """Jede Figur ist ein eigener Bekannter — das ist der ganze Reiz."""
    mem.save(
        "Hamburger", fakten=["Wohnt in Aachen."], zusammenfassung="Ging um den Garten.", jetzt=JETZT
    )

    assert mem.prompt_block("Berlinerin", JETZT) == ""
    assert "Aachen" in mem.prompt_block("Hamburger", JETZT)


def test_gespraech_verfaellt_nach_zwei_wochen_die_fakten_nicht(mem):
    mem.save("Hamburger", fakten=["Fährt Rad."], zusammenfassung="Ging um die Tour.", jetzt=JETZT)

    frisch = mem.prompt_block("Hamburger", JETZT + timedelta(days=13))
    alt = mem.prompt_block("Hamburger", JETZT + timedelta(days=15))

    assert "Beim letzten Mal" in frisch
    assert "Beim letzten Mal" not in alt
    assert "Fährt Rad." in alt  # Fakten bleiben


def test_fakten_werden_ergaenzt_nicht_ersetzt(mem):
    """Sonst verlernt die Figur bei jedem Anruf alles, was diesmal nicht
    zur Sprache kam."""
    mem.save("Hamburger", fakten=["Fährt Rad."], zusammenfassung="a", jetzt=JETZT)
    mem.save(
        "Hamburger", fakten=["Mag Rotwein."], zusammenfassung="b", jetzt=JETZT + timedelta(days=1)
    )

    block = mem.prompt_block("Hamburger", JETZT + timedelta(days=1))

    assert "Fährt Rad." in block and "Mag Rotwein." in block


def test_doppelte_fakten_sammeln_sich_nicht_an(mem):
    for i in range(5):
        mem.save(
            "Hamburger", fakten=["Fährt Rad."], zusammenfassung="x", jetzt=JETZT + timedelta(days=i)
        )

    assert mem.load("Hamburger")["fakten"] == ["Fährt Rad."]


def test_fakten_sind_gedeckelt(mem):
    """Das Gedaechtnis wandert in den System-Prompt — ein aufgeblaehter
    Prompt verwaessert die Verhaltensregeln."""
    mem.save(
        "Hamburger", fakten=[f"Fakt {i}." for i in range(20)], zusammenfassung="x", jetzt=JETZT
    )

    assert len(mem.load("Hamburger")["fakten"]) == MAX_FAKTEN


def test_neueste_fakten_ueberleben_das_kuerzen(mem):
    mem.save(
        "Hamburger",
        fakten=[f"Alt {i}." for i in range(MAX_FAKTEN)],
        zusammenfassung="x",
        jetzt=JETZT,
    )
    mem.save(
        "Hamburger", fakten=["Ganz neu."], zusammenfassung="x", jetzt=JETZT + timedelta(days=1)
    )

    fakten = mem.load("Hamburger")["fakten"]
    assert fakten[0] == "Ganz neu."
    assert len(fakten) == MAX_FAKTEN


def test_prompt_enthaelt_das_gedaechtnis_nur_wenn_es_eines_gibt():
    ohne = full_instructions(ROLES[0])
    mit = full_instructions(ROLES[0], "# Was du vom Anrufer schon weißt\n- Mag Rotwein.")

    assert "Was du vom Anrufer schon weißt" not in ohne
    assert "Mag Rotwein." in mit
    # Die Verhaltensregeln duerfen dadurch nicht verdraengt werden
    assert "ANTWORTE KURZ" in mit and "FRAG NICHT NACH DEM NAMEN" in mit
