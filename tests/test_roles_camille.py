from kitelefon.roles import full_instructions, get_role


def test_ziffer_3_ist_camille():
    rolle = get_role(3)
    assert rolle["name"] == "Camille"
    assert rolle["voice_id"] == "shimmer"
    assert rolle.get("sprachkurs") is True


def test_camille_bekommt_nur_kurs_und_wetter_werkzeuge():
    namen = {s["name"] for s in get_role(3)["tool_schemas"]}
    assert namen == {"note_error", "get_news_fr", "get_weather"}


def test_full_instructions_bypass_fuer_den_sprachkurs():
    rolle = get_role(3)
    fertig = "# Wer du bist\nDu bist Camille…\nLevel: 4\n"
    out = full_instructions(rolle, fertig)
    assert out == fertig.strip()
    # nichts vom Telefon-Gerippe dahinter:
    assert "delegate_task" not in out and "spotify" not in out.lower()


def test_andere_rollen_unveraendert():
    hamburger = get_role(1)
    out = full_instructions(hamburger, "")
    assert "# Rolle" in out and "delegate_task" in out
