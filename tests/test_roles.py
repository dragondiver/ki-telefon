# tests/test_roles.py
import pytest

from kitelefon.roles import DELEGATION_SUFFIX, ROLES, full_instructions, get_role


def test_roles_has_nine_entries():
    assert len(ROLES) == 9


def test_each_role_has_required_keys():
    for r in ROLES:
        assert {"name", "voice_id", "gpt_style"} <= set(r.keys())


def test_get_role_returns_by_index_1_based():
    assert get_role(1) is ROLES[0]
    assert get_role(9) is ROLES[8]


def test_get_role_out_of_range_raises():
    with pytest.raises(ValueError):
        get_role(0)
    with pytest.raises(ValueError):
        get_role(10)


def test_all_roles_include_delegation_suffix_in_style():
    assert "delegate_task" in DELEGATION_SUFFIX
    assert len(DELEGATION_SUFFIX) > 50


def test_jede_rolle_hat_ein_sprechtempo():
    """speed wird seit 2026-08-03 wirklich gesendet — fehlt es, spricht die
    Rolle stumm auf Vorgabetempo weiter, was niemand bemerkt."""
    for r in ROLES:
        if r.get("sprachkurs"):
            # Camille bringt ihren Prompt fertig aus dem sprachkurs-Repo mit
            # (full_instructions() bricht dort frueh ab) und hat deshalb kein
            # eigenes speed-Feld — das Sprachtempo steuert dort der Kurs.
            continue
        assert 0.25 <= r["speed"] <= 1.5, r["name"]


# --- Regeln, die im alten Prompt verletzt waren -------------------------


def test_keine_rolle_haengt_an_einem_eigenen_modell():
    """Seit 2026-08-03 laeuft alles auf einem gemeinsamen Modell (damals
    gpt-realtime-2.1, seit #131 gpt-realtime-2.1-mini). Ein vergessener
    Pin wuerde eine einzelne Rolle still auf einem aelteren Modell lassen —
    genau das war vorher bei Marty und der KI-Rolle der Fall."""
    gepinnt = {r["name"]: r["model"] for r in ROLES if "model" in r}
    assert not gepinnt, f"Rollen mit eigenem Modell: {gepinnt}"


def test_niemand_fragt_mehr_nach_dem_namen():
    """Der alte Prompt verlangte den Namen zweimal pro Variante und
    versprach dafuer Personeninfos, die es im Code nie gab."""
    for r in ROLES:
        if r.get("sprachkurs"):
            # Camille bekommt keine Telefon-Vorlage, siehe test_roles_camille.py
            continue
        text = full_instructions(r)
        assert "FRAG NICHT NACH DEM NAMEN" in text, r["name"]
        assert "Erfrage den Namen" not in text, r["name"]
        assert "weitere Informationen der Person" not in text, r["name"]


def test_kein_widerspruch_zwischen_fragen_und_nicht_fragen():
    """Alt: 'Beende Deine Antwort nicht mit einer Frage' stand direkt neben
    zwei Aufforderungen zu fragen. Laut OpenAI-Leitfaden der teuerste Fehler."""
    for r in ROLES:
        text = full_instructions(r)
        assert "Beende Deine Antwort nicht mit einer Frage" not in text, r["name"]


def test_woerterlisten_sind_kurz_und_dosiert():
    """Zweite Runde (2026-08-06): reine Temperamentsbeschreibung ohne jedes
    Wort erzeugte null Anklang (Jean-Luc). Die erste Runde (lange Listen,
    einfach abgearbeitet) erzeugte Karikaturen. Der Mittelweg: 3-5 Wörter,
    aber die Dosierungsregel verbietet, sie stumpf abzuhaken."""
    for r in ROLES:
        if not r.get("faerbung"):
            continue
        woerter = r.get("woerter", [])
        assert 3 <= len(woerter) <= 5, f"{r['name']}: {len(woerter)} Wörter"
        text = full_instructions(r)
        assert "nicht in jeder Antwort, aber regelmäßig genug" in text, r["name"]


def test_gefaerbte_rollen_bekommen_die_herkunftsregel():
    for r in ROLES:
        text = full_instructions(r)
        if r.get("faerbung"):
            assert "KEINE Parodie" in text, r["name"]
            for wort in r.get("woerter", []):
                assert wort in text, f"{r['name']}: '{wort}' fehlt im Prompt"
        else:
            assert "KEINE Parodie" not in text, r["name"]


def test_gefaerbte_rollen_haben_eine_lautliche_anweisung():
    """Runde 2 scheiterte an reiner Beschreibung ("Akzent in der
    Satzmelodie") — die Sprachsynthese bekam kein Signal, wann sie
    umschalten soll. Der akzent-Text muss LAUTLICH sein und durchgehend
    gelten, nicht nur bei den Streuwörtern."""
    for r in ROLES:
        if not r.get("faerbung"):
            continue
        assert r.get("akzent"), f"{r['name']}: kein Aussprache-Abschnitt"
        text = full_instructions(r)
        assert "# So klingst du" in text, r["name"]
        assert "SPRICH DIESEN AKZENT DURCHGEHEND" in text, r["name"]


def test_gefaerbte_rollen_haben_musterzeilen():
    """Runde 3 (lautliche Regeln) reichte auch nicht — der Anrufer sagte
    im Protokoll "es hört sich sehr hochdeutsch an". Beispielsaetze in
    Akzent-Schreibweise sind laut Leitfaden die wirksamste Technik."""
    for r in ROLES:
        if not r.get("faerbung"):
            continue
        beispiele = r.get("beispiele", [])
        assert 2 <= len(beispiele) <= 4, f"{r['name']}: {len(beispiele)}"
        text = full_instructions(r)
        for b in beispiele:
            assert b in text, f"{r['name']}: Musterzeile fehlt"


def test_akzent_steht_vor_den_gespraechsregeln():
    """Weiter unten ging der Abschnitt zwischen den uebrigen Regeln
    unter — deshalb steht er jetzt direkt hinter der Figur."""
    for r in ROLES:
        if not r.get("faerbung"):
            continue
        text = full_instructions(r)
        assert text.index("# So klingst du") < text.index("# Gespräch am Telefon"), r["name"]


def test_deutsche_rollen_duzen_der_anrufer_ist_kein_kunde():
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        if r.get("sprache") == "ENGLISCH":
            # Englisch kennt die Unterscheidung nicht — die Regel waere Ballast.
            assert "DUZE DEN ANRUFER" not in text, r["name"]
        else:
            assert "DUZE DEN ANRUFER" in text, r["name"]


def test_ganze_fremdsprachige_saetze_sind_verboten():
    """Jean-Luc sagte "Ah, vous ressentez also Stress, non?" — einzelne
    Woerter sind erlaubt, dieser Satzteil nicht."""
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        assert "GANZE SÄTZE ODER SATZTEILE NICHT" in text, r["name"]


def test_nur_die_ki_rolle_darf_sich_zu_erkennen_geben():
    """Alt galt 'Gib dich nicht als KI zu erkennen' auch fuer die Rolle,
    die woertlich eine kuenstliche Intelligenz ist."""
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        if r.get("ist_ki"):
            assert "nicht als KI" not in text, r["name"]
        else:
            assert "nicht als KI" in text, r["name"]


def test_regel_fuer_unverstaendliche_audio_ist_ueberall_drin():
    """Kohlemikrofon von 1963: ohne diese Regel raet das Modell drauflos."""
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        assert "UNVERSTÄNDLICH" in text, r["name"]
        assert "ERWÄHNE NIE Technik" in text, r["name"]


def test_laengenregel_und_eine_frage_pro_antwort():
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        assert "ANTWORTE KURZ" in text, r["name"]
        assert "HÖCHSTENS EINE FRAGE" in text, r["name"]


def test_alle_werkzeuge_sind_in_jedem_prompt_erklaert():
    for r in ROLES:
        if r.get("sprachkurs"):
            # Camille sieht nur ihre drei Kurs-Werkzeuge, siehe
            # tools.SPRACHKURS_TOOL_SCHEMAS — diese Regel gilt fuer sie nicht.
            continue
        text = full_instructions(r)
        for tool in ("delegate_task", "spotify_play", "suggest_recipe", "modify_recipe"):
            assert tool in text, f"{r['name']}: {tool} fehlt"


def test_sprache_wird_festgenagelt():
    """Ohne Festlegung driften Figuren mit auslaendischer Herkunft in ihre
    Herkunftssprache ab. Ravi soll umgekehrt bei Englisch bleiben, obwohl
    die uebrigen Prompt-Abschnitte auf Deutsch stehen."""
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        erwartet = r.get("sprache", "DEUTSCH")
        assert f"SPRICH DURCHGEHEND {erwartet}" in text, r["name"]
        assert "Wechsle die Sprache auch dann nicht" in text, r["name"]


def test_genau_eine_rolle_spricht_englisch():
    englisch = [r for r in ROLES if r.get("sprache") == "ENGLISCH"]
    assert len(englisch) == 1
    assert englisch[0]["name"] == "Java-Entwickler aus Indien"
    # Persoenlichkeit und Klang stehen bei ihm auf Englisch, damit der
    # Prompt die Sprache nicht selbst unterlaeuft.
    assert "Java developer" in englisch[0]["gpt_style"]


def test_prompt_ist_in_abschnitte_gegliedert():
    """Der Leitfaden empfiehlt beschriftete Abschnitte statt Fliesstext."""
    text = full_instructions(ROLES[0])
    for abschnitt in ("# Rolle", "# Dein Auftreten", "# Gespräch am Telefon", "# Werkzeuge"):
        assert abschnitt in text


def test_prompt_und_werkzeug_sind_sich_ueber_die_zustellung_einig():
    """Frueher ein Widerspruch, jetzt andersherum aufgeloest.

    Bis 2026-08-13 hiess es an beiden Stellen "die Zustellung ist noch nicht
    gebaut" — inzwischen gibt es sie. Ein Prompt, der das Gegenteil behauptet,
    faellt sonst erst am Telefon auf. Widersprueche zwischen Rollen-Prompt und
    Werkzeugbeschreibung sind hier schon zweimal teuer gewesen.
    """
    from kitelefon.tools import DELEGATE_TASK_TOOL_SCHEMA

    beschreibung = DELEGATE_TASK_TOOL_SCHEMA["description"]
    assert "noch nicht gebaut" not in beschreibung
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        assert "noch nicht gebaut" not in text, r["name"]
        assert "Die Zustellung IST gebaut" in text, r["name"]


def test_rezepte_gelten_nicht_als_hintergrundarbeit():
    """Ein Rezeptwunsch landete bei delegate_task, weil "Recherche" als
    Ausloeser dastand und nur das Wetter ausgenommen war."""
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        assert "NIEMALS für Musik, Wetter, Einkaufsliste oder Rezepte" in text, r["name"]


def test_kein_eigenes_rezeptwissen():
    """Jean-Luc improvisierte am 2026-08-06 ein komplettes Pancake-Rezept
    und behauptete, es gespeichert zu haben — ohne suggest_recipe oder
    confirm_recipe je aufzurufen. Die Koch-Rolle verleitet dazu, das
    Werkzeug fuer verzichtbar zu halten."""
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        assert "DU HAST KEIN EIGENES REZEPTWISSEN" in text, r["name"]
        # Die zweite Haelfte des Befunds — behauptet gespeichert, nie
        # aufgerufen — steht seit dem 13.08. als allgemeine Regel weiter
        # oben statt nur im Rezept-Abschnitt. Siehe
        # test_sagen_ist_nicht_tun_gilt_fuer_alle_rollen.
        assert "RUF DAS WERKZEUG AUF" in text, r["name"]


def test_jede_rolle_hat_eigene_einstiege_statt_nur_eines_verbots():
    """Drei Runden Verbot haben "Alles klar" nicht abgestellt — zuletzt
    sechsmal in einem Gespraech. Ein Negativsatz nennt das Wort und macht
    es erst praesent, ohne einen Ersatz anzubieten. Jetzt eine positive
    Liste, aus der abwechselnd gewaehlt wird."""
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        einstiege = r.get("einstiege", [])
        assert len(einstiege) >= 4, f"{r['name']}: nur {len(einstiege)}"
        assert "Alles klar" not in einstiege, r["name"]
        text = full_instructions(r)
        for e in einstiege:
            assert e in text, f"{r['name']}: '{e}' fehlt im Prompt"
        assert "benutze ABWECHSELND diese" in text, r["name"]


def test_rollenbruch_ueber_die_eigene_sprechweise_ist_verboten():
    """Hein erklaerte dem Anrufer seine Sprechweise ("Wir bleiben ganz
    nordisch, trocken") und versprach Besserung — das ist Assistenten-
    Verhalten, kein Fischhaendler."""
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        assert "# Du bleibst in der Rolle" in text, r["name"]
        assert "Sprich NIE über deine eigene Sprechweise" in text, r["name"]


def test_akzent_schreibweise_gilt_fuer_jede_antwort():
    """Runde 5: Wortfetzen sassen, die Aussprache blieb hochdeutsch. Die
    Musterzeilen dienten nur als Lesevorlage — jetzt sollen sie die
    Formulierung JEDER Antwort steuern."""
    for r in ROLES:
        if not r.get("faerbung"):
            continue
        text = full_instructions(r)
        assert "FORMULIERE JEDE ANTWORT INNERLICH IN DIESER SCHREIBWEISE" in text, r["name"]


def test_zustimmung_wird_nicht_verweigert():
    """Jean-Luc weigerte sich auf "dann speicher das" zu speichern —
    die alte Negativregel hatte er als "sag nicht, dass du speicherst"
    gelesen."""
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        assert "Weigere dich NIE zu speichern" in text, r["name"]
        assert 'SAG NIEMALS "ich speichere/notiere das"' not in text, r["name"]


def test_stimmen_sind_gueltig():
    """marin/cedar sind seit dem GA-Release verfuegbar (am Server geprueft).
    Ein Tippfehler hier faellt sonst erst im Gespraech auf."""
    erlaubt = {
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "sage",
        "shimmer",
        "verse",
        "marin",
        "cedar",
    }
    for r in ROLES:
        assert r["voice_id"] in erlaubt, f"{r['name']}: {r['voice_id']}"


def test_rezeptdetails_muessen_vorgelesen_statt_erfunden_werden():
    """Das Werkzeug liefert seit 2026-08-08 zutaten und schritte mit — der
    Prompt muss auch sagen, dass daraus vorzulesen ist."""
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        assert "LIES AUS DIESEN FELDERN vor" in text, r["name"]


def test_delegation_verbietet_zusage_ohne_werkzeug():
    """Am 13.08. sagte der Hamburger dreimal 'notiert' und rief nie auf."""
    from kitelefon.roles import DELEGATION_SUFFIX

    # Die alte Notluege darf nicht zurueckkommen
    assert "noch nicht gebaut" not in DELEGATION_SUFFIX
    assert "FRAG NICHT nach Email" not in DELEGATION_SUFFIX


def test_delegation_fragt_nach_dem_zustellweg():
    from kitelefon.roles import DELEGATION_SUFFIX

    assert "'callback'" in DELEGATION_SUFFIX and "'email'" in DELEGATION_SUFFIX
    # Die Namen der Empfaenger kommen aus KITELEFON_PERSONEN (#149, test_personen.py).
    assert "Bei E-Mail auch,\n  an wen: " in DELEGATION_SUFFIX


def test_sagen_ist_nicht_tun_gilt_fuer_alle_rollen():
    """Die Regel stand ein Jahr lang nur bei den Rezepten.

    Am 13.08. sagte der Hamburger dreimal 'hab ich notiert' und rief
    delegate_task nie auf. Eine Regel, die nur in einem Abschnitt steht,
    gilt fuer das Modell auch nur dort.
    """
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        assert "SAGEN IST NICHT TUN" in text, r["name"]
        assert "Erst wenn das Werkzeug geantwortet hat" in text, r["name"]


def test_die_regel_steht_nur_einmal():
    """Doppelt gepflegte Regeln laufen auseinander."""
    text = full_instructions(ROLES[0])
    assert text.count("SAGEN IST NICHT TUN") == 1


def test_eigennamen_werden_zurueckgelesen():
    """Am 14.08. wurde aus 'Devon' ein 'Berlin'.

    Zweimal verhoert an zwei Stellen: die Transkription machte 'Deben'
    daraus, das Modell 'Berlin' — und schrieb es in die Aufgabe, obwohl im
    selben Satz 'in England' stand. Die Korrektur kostete zwei Anrufe.
    """
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        assert "ORTS- UND EIGENNAMEN" in text, r["name"]
        assert "PRÜFE IHN GEGEN DEN REST" in text, r["name"]


def test_nach_einer_rueckfrage_wird_gewartet():
    """Holger fragte am 14.08. nach dem Zustellweg und beantwortete sich
    die Frage 1,3 Sekunden spaeter selbst — zweimal, bevor Florian dran war.
    """
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        text = full_instructions(r)
        assert "WER FRAGT, HÖRT AUF ZU REDEN" in text, r["name"]


def test_aufgabe_wird_vor_dem_anlegen_zurueckgesagt():
    from kitelefon.roles import DELEGATION_SUFFIX

    assert "sag die Aufgabe in einem Satz zurück" in DELEGATION_SUFFIX
    assert "DIESE FRAGE STELLST DU VOR DEM AUFRUF" in DELEGATION_SUFFIX


def test_reise_ohne_abfahrtsort_wird_nachgefragt():
    """Am 14.08. ging 'Verbindung nach Bullerbue' ohne Startort raus.

    Cowork hat die Luecke erkannt und nachgefragt — per Mail, drei Minuten
    spaeter. Die Frage gehoert ins Gespraech, wo sie zwei Sekunden kostet.
    """
    from kitelefon.roles import DELEGATION_SUFFIX

    assert "EINE AUFGABE MUSS VOLLSTÄNDIG SEIN" in DELEGATION_SUFFIX
    assert "VON WO" in DELEGATION_SUFFIX


def test_eine_frage_gilt_auch_innerhalb_eines_satzes():
    """Astrid fragte am 14.08. nach Personenzahl UND Zeitraum in einem Satz.

    Sie bekam die Personenzahl, legte die Aufgabe sofort an — der Zeitraum
    kam eine Sekunde spaeter und fehlte in der Aufgabe. Die Regel gab es
    schon; sie war nur nicht auf den Satz gemuenzt, sondern auf die Antwort.
    """
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        assert "Das gilt auch INNERHALB" in full_instructions(r), r["name"]


def test_zustellweg_laesst_sich_nachtraeglich_aendern():
    from kitelefon.roles import DELEGATION_SUFFIX

    assert "zustellung_aendern" in DELEGATION_SUFFIX


def test_bote_liest_die_beschreibung_nicht_woertlich():
    """Am 14.08.: "…von Stuttgart nach London für zwei Personen SUCHEN".

    Die Beschreibung ist eine Notiz, kein Satz. Woertlich vorgelesen klingt
    sie nach abgelesenem Zettel.
    """
    from kitelefon.roles import bote_rolle

    text = full_instructions(bote_rolle("Zugverbindung suchen", "Ergebnis hier"))
    assert "IN EIGENEN WORTEN, NICHT WOERTLICH" in text


def test_bote_liest_vor_wenn_niemand_antwortet():
    """Die Rueckfrage ist eine Hoeflichkeit, das Ergebnis ist der Zweck."""
    from kitelefon.roles import bote_rolle

    text = full_instructions(bote_rolle("Zugverbindung suchen", "Ergebnis hier"))
    assert "GAR KEINE Antwort" in text


# Orte und Reiseaufgaben, die als Beispiele in den Prompt-Regeln stehen. Wer
# sie im Rueckruf-Prompt findet, kann sie fuer das Ergebnis halten.
_BEISPIELE_AUS_DEN_REGELN = ("Devon", "London", "Bullerbü", "Zugverbindung")


@pytest.mark.parametrize("basis", ["", *[r["name"] for r in ROLES if not r.get("sprachkurs")]])
def test_rueckruf_prompt_enthaelt_keine_beispielorte(basis):
    """Am 3.10. las das Telefon bei einem Rueckruf zu Souvignier Gris
    Zugverbindungen nach Devon vor — beides stand nur als Regel-Beispiel im
    Prompt (Aufgaben-Rueckfrage, Eigennamen), nicht im Ergebnis. Im Rueckruf
    gibt es kein Delegieren und keine Rueckfrage zu Namen; dort darf nichts
    stehen, was das Modell fuer den Inhalt halten kann.
    """
    from kitelefon.roles import bote_rolle

    ergebnis = "Souvignier Gris ist eine pilzwiderstandsfaehige Weissweinrebe."
    text = full_instructions(bote_rolle("Souvignier Gris", ergebnis, basis=basis))
    # Alles ausser dem Ergebnis-Abschnitt selbst durchsuchen.
    regeln = text.split("--- ERGEBNIS ANFANG ---")[0]
    for beispiel in _BEISPIELE_AUS_DEN_REGELN:
        assert beispiel not in regeln, f"{beispiel!r} steht im Rueckruf-Prompt ({basis or 'KI'})"


def test_delegation_suffix_nennt_web_suche_fuer_schnelle_fakten():
    assert "web_suche" in DELEGATION_SUFFIX
    assert "OHNE AUSNAHME" in DELEGATION_SUFFIX


def test_nachschauen_geht_zuerst_an_web_suche_und_dann_an_delegate_task():
    """Florian, 05.10.2026: „schau nach / recherchier das / finde das raus“ bei einer
    schnellen Faktenfrage zuerst web_suche. Zweifel des Anrufers, ein Fehler der Suche
    oder ein ausdrücklicher Wunsch nach Rückruf bleiben bei delegate_task (Oleander, 13.08.)."""
    nachschauen = next(
        punkt
        for punkt in DELEGATION_SUFFIX.split("\n- ")
        if '"recherchier das"' in punkt.replace("\n  ", " ")
    )
    flach = " ".join(nachschauen.split())
    assert flach.index("web_suche") < flach.index("delegate_task")
    assert "Zweifelt" in flach and "OHNE AUSNAHME" in flach
    # Die ausdrücklichen Rückruf-Wünsche bleiben eine eigene, harte Regel.
    hart = next(p for p in DELEGATION_SUFFIX.split("\n- ") if "ruf mich zurück" in p)
    assert "IMMER delegate_task" in " ".join(hart.split())
    assert "recherchier" not in hart


def test_laenderspiel_ohne_zusatz_ist_deutschland():
    """#118: Das Modell fragte bei "Länderspiel" nach den Mannschaften (nur 11 von 20 Aufrufen)."""
    from kitelefon.roles import DELEGATION_SUFFIX

    assert '"Länderspiel" ohne Zusatz heißt: ein Spiel der deutschen Nationalmannschaft' in (
        DELEGATION_SUFFIX
    )
    assert "frag NICHT nach den Mannschaften, ruf sofort web_suche" in DELEGATION_SUFFIX
    assert "zwei\n  andere Mannschaften" in DELEGATION_SUFFIX
    assert "Länderspiel gegen Frankreich" in DELEGATION_SUFFIX
    assert DELEGATION_SUFFIX in full_instructions(
        next(r for r in ROLES if r["name"] == "Künstliche Intelligenz")
    )


def test_gedaechtnis_wird_nicht_delegiert():
    """#122: Aufräumen und Löschen im Gedächtnis erledigt das Modell selbst,
    auch bei "übernimm das" oder "kümmer dich drum"."""
    assert "Haushaltsgedächtnis" in DELEGATION_SUFFIX
    assert "erledigst du selbst mit merke, erinnere und\n  vergiss" in DELEGATION_SUFFIX
    assert "‚übernimm das‘" in DELEGATION_SUFFIX
    for r in ROLES:
        if r.get("sprachkurs"):
            continue
        assert "Haushaltsgedächtnis" in full_instructions(r), r["name"]
