"""Die Themenliste des Haushalts steht im Gedaechtnis-Block des Prompts."""

import sqlite3
from datetime import UTC, datetime

from kitelefon.__main__ import gedaechtnis_fuer
from kitelefon.haushalt import Haushalt
from kitelefon.memory import RoleMemory
from kitelefon.roles import ROLES, full_instructions


def test_ohne_beides_ist_der_block_leer():
    assert gedaechtnis_fuer(None, None, "Hamburger") == ""


def test_leerer_haushalt_fuegt_nichts_hinzu(tmp_path):
    assert gedaechtnis_fuer(None, Haushalt(tmp_path / "tasks.db"), "Hamburger") == ""


def test_themenzeile_kommt_zum_rollengedaechtnis_dazu(tmp_path):
    jetzt = datetime.now(UTC)
    memory = RoleMemory(tmp_path / "tasks.db")
    memory.save("Hamburger", fakten=["Mag keinen Koriander."], zusammenfassung="", jetzt=jetzt)
    haushalt = Haushalt(tmp_path / "tasks.db")
    haushalt.merke("Urlaub Italien", "Hotel in Rimini ist gebucht.", jetzt)

    block = gedaechtnis_fuer(memory, haushalt, "Hamburger")

    assert "Mag keinen Koriander." in block
    assert "Urlaub Italien (1)" in block
    assert "Rimini" not in block


def test_themenzeile_steht_im_fertigen_prompt(tmp_path):
    haushalt = Haushalt(tmp_path / "tasks.db")
    haushalt.merke("Urlaub Italien", "Hotel gebucht.", datetime.now(UTC))

    prompt = full_instructions(ROLES[0], gedaechtnis_fuer(None, haushalt, ROLES[0]["name"]))

    assert "Urlaub Italien (1)" in prompt


class _KaputterHaushalt:
    def themenzeile(self):
        raise sqlite3.DatabaseError("database disk image is malformed")


def test_kaputte_haushalt_db_blockiert_das_telefonat_nicht(tmp_path):
    assert gedaechtnis_fuer(None, _KaputterHaushalt(), "Hamburger") == ""

    memory = RoleMemory(tmp_path / "tasks.db")
    memory.save(
        "Hamburger", fakten=["Mag keinen Koriander."], zusammenfassung="", jetzt=datetime.now(UTC)
    )
    block = gedaechtnis_fuer(memory, _KaputterHaushalt(), "Hamburger")
    assert "Mag keinen Koriander." in block


def test_prompt_verlangt_sofortigen_merke_aufruf():
    prompt = full_instructions(ROLES[0], "")

    assert "Ruf SOFORT merke auf" in prompt
    assert "Urlaub Italien" in prompt
