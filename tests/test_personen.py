"""Namen aus dem Haushalt kommen aus der .env, nicht aus dem Code (#149).

Mit den Namen vom Gerät müssen die Textbausteine genau das ergeben, was
vorher fest im Code stand. Die Vergleichstexte unten sind die alten
Zeilen, mit Platzhalternamen statt der echten.
"""

import re

from kitelefon.personen import (
    andere_empfaenger,
    empfaenger_auswahl,
    namen_liste,
    spotify_konten_satz,
)


def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_namen_liste_normalisiert():
    assert namen_liste(" Paula, otto ,,paula ") == ["paula", "otto"]
    assert namen_liste("") == []


def test_empfaenger_wie_vorher():
    # vorher: "'paula', 'otto' oder eine Adresse" / "... oder eine Mailadresse"
    assert (
        empfaenger_auswahl(["paula", "otto"], "eine Adresse") == "'paula', 'otto' oder eine Adresse"
    )
    assert empfaenger_auswahl(["paula", "otto"], "eine Mailadresse") == (
        "'paula', 'otto' oder eine Mailadresse"
    )


def test_empfaenger_ohne_namen_nur_adresse():
    assert empfaenger_auswahl([], "eine Mailadresse") == "eine Mailadresse"


def test_andere_empfaenger_wie_vorher():
    # vorher: "an dich, an Otto, oder eine andere\n  Adresse."
    assert andere_empfaenger(["paula", "otto"]) == "an dich, an Otto, oder eine andere Adresse."
    assert andere_empfaenger(["paula"]) == "an dich oder eine andere Adresse."
    assert andere_empfaenger([]) == "an dich oder eine andere Adresse."


def test_spotify_satz_wie_vorher():
    # vorher: "Zwei getrennte Konten im Haushalt: Paula und Otto."
    assert (
        spotify_konten_satz(["paula", "otto"])
        == "Zwei getrennte Konten im Haushalt: Paula und Otto."
    )
    assert spotify_konten_satz(["paula", "paula"]) == "Ein getrenntes Konto im Haushalt: Paula."


def test_prompts_ohne_platzhalter():
    """Kein Platzhalter bleibt im fertigen Prompt oder Schema stehen."""
    from kitelefon import roles, tools

    texte = [roles.DELEGATION_SUFFIX, roles.SPOTIFY_SUFFIX, roles._BOTE_AUFTRAG]
    texte += [str(s) for s in tools.ALL_TOOL_SCHEMAS]
    for t in texte:
        for platzhalter in ("__EMPFAENGER__", "__SPOTIFY_KONTEN__", "__ANDERE_EMPFAENGER__"):
            assert platzhalter not in t


def test_sprachkurs_ohne_lernende_meldet_laut(caplog):
    from types import SimpleNamespace

    import pytest

    from kitelefon.sprachkurs_call import SprachkursNichtVerfuegbar, start_sprachkurs_call

    with pytest.raises(SprachkursNichtVerfuegbar):
        start_sprachkurs_call(SimpleNamespace(sprachkurs_lernende=""))
    assert "KITELEFON_SPRACHKURS_LERNENDE" in caplog.text


def test_spool_ohne_lernende_bleibt_liegen(tmp_path, caplog):
    from types import SimpleNamespace

    from kitelefon.sprachkurs_call import spool_nachtragen

    (tmp_path / "s1.json").write_text("{}", encoding="utf-8")
    settings = SimpleNamespace(sprachkurs_lernende="", sprachkurs_spool_dir=tmp_path)
    assert spool_nachtragen(settings, queue=None) == {"nachgetragen": 0, "fehlgeschlagen": 1}
    assert (tmp_path / "s1.json").exists()
    assert "KITELEFON_SPRACHKURS_LERNENDE" in caplog.text


def test_prompts_nutzen_die_bausteine():
    """Die Prompts setzen genau die Bausteine mit den Namen aus der .env ein."""
    from kitelefon import roles
    from kitelefon.personen import EMPFAENGER, SPOTIFY_KONTEN

    assert (
        "an wen: " + empfaenger_auswahl(EMPFAENGER, "eine Adresse") + "." in roles.DELEGATION_SUFFIX
    )
    assert "- " + spotify_konten_satz(SPOTIFY_KONTEN) + "\n" in roles.SPOTIFY_SUFFIX
    assert "frag nach: " + andere_empfaenger(EMPFAENGER) in roles._BOTE_AUFTRAG
