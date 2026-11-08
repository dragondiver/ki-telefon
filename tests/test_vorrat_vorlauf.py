"""Der Prefetch darf nicht im Anrufstart-Stau starten.

Sitzungen 9 (19.08.) und 10 (22.08.): Beim Anrufstart lief die
Wetterabfrage in ConnectTimeout, waehrend dieselbe API vom selben Pi in
0,1 s antwortete (dreimal gemessen, HTTP 200). Der Aufbau —
Realtime-Handshake, Audio-Streams, Kontextblock aus Postgres — laesst den
Verbindungsaufbau nicht durch, bevor der 3-s-Timeout greift. Der Vorrat
wird erst gebraucht, wenn Camille im Gespraech ein Werkzeug ruft, also
startet der Prefetch mit Vorlauf.
"""

from kitelefon import __main__ as hauptmodul


def test_vorlauf_ist_gesetzt_und_plausibel():
    # Genug Abstand, damit der Anrufaufbau durch ist; klein genug, dass der
    # Vorrat vor dem ersten Werkzeugaufruf bereitsteht (Camille braucht die
    # ersten Saetze fuer die Begruessung).
    assert 3.0 <= hauptmodul.VORRAT_VORLAUF_S <= 15.0


def test_vorrat_fuellen_wartet_vor_dem_ersten_abruf():
    """Der Prefetch schlaeft, bevor er das Netz anfasst — sonst konkurriert
    er mit dem Sitzungsaufbau um denselben Event-Loop."""
    import inspect

    quelle = inspect.getsource(hauptmodul)
    start = quelle.index("async def _vorrat_fuellen")
    rumpf = quelle[start : start + 700]
    schlaf = rumpf.index("asyncio.sleep(VORRAT_VORLAUF_S)")
    wetter = rumpf.index("handle_get_weather(args)")
    assert schlaf < wetter, "Der Vorlauf muss VOR dem ersten Abruf liegen"
