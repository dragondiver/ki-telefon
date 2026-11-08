import asyncio

import pytest


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def ohne_kitelefon_env(monkeypatch):
    """Alle KITELEFON_*-Variablen aus der Prozessumgebung entfernen.

    agents/spotify.py und agents/recipes.py rufen beim Import load_dotenv()
    auf; sobald ein frueherer Test sie importiert, steht die echte .env im
    Prozess. Tests, die Default-Werte von Settings pruefen, wuerden dann auf
    einem Rechner mit .env (dem Pi) rot, in CI aber gruen — telefon-ai#56.
    """
    import os

    for name in [k for k in os.environ if k.startswith("KITELEFON_")]:
        monkeypatch.delenv(name, raising=False)
