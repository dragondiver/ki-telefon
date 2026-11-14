"""destilliere(): ohne Worte vom Anrufer kein Aufruf ans Modell (#21)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kitelefon.agents import erinnerung
from kitelefon.agents.erinnerung import Erinnerung, destilliere

BEGRUESSUNG = (
    "Schwabe",
    "Ha no, i sag amol, hier isch Holger aus Schwaben. Des passt fei. Was hammer "
    "denn in der Runde jetzt grad? Lass mal hören, was du soweils brauchst.",
)


class _FakeMessages:
    def __init__(self) -> None:
        self.aufrufe = 0
        self.kwargs: dict = {}
        self.stop_reason = "end_turn"

    async def parse(self, **kwargs):
        self.aufrufe += 1
        self.kwargs = kwargs
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            parsed_output=Erinnerung(fakten=["Mag Metal."], zusammenfassung="Musik."),
        )


@pytest.fixture
def fake_client(monkeypatch):
    messages = _FakeMessages()
    monkeypatch.setattr(
        erinnerung, "_get_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=messages))
    )
    return messages


async def test_nur_begruessung_der_figur_ergibt_keine_erinnerung(fake_client):
    # Am 14.11.2026 wurde daraus "Heißt Holger." und "Kommt aus Schwaben."
    assert await destilliere([BEGRUESSUNG]) is None
    assert fake_client.aufrufe == 0


async def test_kurzes_hallo_vom_anrufer_reicht_nicht(fake_client):
    verlauf = [BEGRUESSUNG, ("Anrufer", "Holger, kannst du mich gut hören?")]
    assert await destilliere(verlauf) is None
    assert fake_client.aufrufe == 0


async def test_anrufer_erzaehlt_etwas_dann_wird_destilliert(fake_client):
    verlauf = [
        BEGRUESSUNG,
        ("Anrufer", "Ich höre gern Metal, vor allem AC/DC, am liebsten T.N.T. ganz laut."),
    ]
    ergebnis = await destilliere(verlauf)
    assert ergebnis is not None
    assert ergebnis.fakten == ["Mag Metal."]
    assert fake_client.aufrufe == 1


async def test_neues_modell_mit_aufwand_und_ersatzmodell(fake_client):
    verlauf = [
        BEGRUESSUNG,
        ("Anrufer", "Ich höre gern Metal, vor allem AC/DC, am liebsten ganz laut."),
    ]
    await destilliere(verlauf)
    assert fake_client.kwargs["model"] == "claude-opus-5-5"
    assert fake_client.kwargs["output_config"] == {"effort": "low"}
    assert fake_client.kwargs["fallbacks"] == "default"
    assert "thinking" not in fake_client.kwargs  # abschalten gibt bei 5.5 einen 400


async def test_ablehnung_ergibt_keine_erinnerung(fake_client):
    fake_client.stop_reason = "refusal"
    verlauf = [
        BEGRUESSUNG,
        ("Anrufer", "Ich höre gern Metal, vor allem AC/DC, am liebsten ganz laut."),
    ]
    assert await destilliere(verlauf) is None
