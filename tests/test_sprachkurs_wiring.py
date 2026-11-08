from kitelefon.tasks import VALID_AGENT_TYPES


def test_agent_type_ist_registriert():
    assert "sprachkurs_auswertung" in VALID_AGENT_TYPES
