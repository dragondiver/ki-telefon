"""Der Schliessen-und-neu-Oeffnen-Pfad des Mikrofon-Streams (#25).

``restart_input()`` ist die Selbstheilung hinter dem Mikro-Wachhund und
hatte bislang keinen direkten Test — geprueft wurde nur, *dass* der
Wachhund sie ruft, nie *was* sie tut. Moeglich wird das durch die Naht
``AudioIO._oeffne_input_stream()``: die einzige Stelle, die PortAudio
anfasst, laesst sich hier ersetzen.
"""

import asyncio

import pytest

from kitelefon.audio import AudioIO


class FakeStream:
    """Zaehlt, was der echte PortAudio-Stream ueber sich ergehen laesst."""

    def __init__(self, kaputt: bool = False):
        self.kaputt = kaputt
        self.gestartet = 0
        self.gestoppt = 0
        self.geschlossen = 0

    def start(self):
        self.gestartet += 1

    def stop(self):
        self.gestoppt += 1
        if self.kaputt:
            raise RuntimeError("Stream verklemmt")

    def close(self):
        self.geschlossen += 1


class FakeAudioIO(AudioIO):
    """AudioIO ohne PortAudio — die Naht als Unterklasse ausgenutzt."""

    def __init__(self):
        super().__init__()
        self.streams: list[FakeStream] = []

    def _oeffne_input_stream(self):
        s = FakeStream()
        self.streams.append(s)
        return s


@pytest.fixture
def audio():
    return FakeAudioIO()


@pytest.mark.asyncio
async def test_restart_schliesst_den_alten_und_oeffnet_einen_neuen(audio):
    audio.start_input()
    alt = audio.streams[0]

    audio.restart_input()

    assert alt.gestoppt == 1 and alt.geschlossen == 1
    assert len(audio.streams) == 2
    assert audio.streams[1].gestartet == 1
    assert audio.streams[1] is not alt


@pytest.mark.asyncio
async def test_restart_oeffnet_auch_wenn_der_alte_sich_nicht_schliessen_laesst(audio):
    """Genau der Fall, fuer den es die Selbstheilung gibt.

    Ein Stream, der still gestorben ist, kann beim stop() werfen. Wenn
    das den Neustart verhindern wuerde, bliebe das Telefon taub — die
    Ausnahme wird deshalb geschluckt und trotzdem neu geoeffnet.
    """
    audio.start_input()
    audio.streams[0].kaputt = True

    audio.restart_input()

    assert len(audio.streams) == 2
    assert audio.streams[1].gestartet == 1


@pytest.mark.asyncio
async def test_restart_setzt_den_frame_zeitstempel_zurueck(audio):
    audio.start_input()
    audio._letzter_mic_frame = 12345.0

    audio.restart_input()

    # Der frische Stream hat noch nie geliefert — der Wachhund muss das
    # als "noch kein Frame" sehen und nicht als uraltes Alter.
    assert audio.sekunden_seit_mic_frame() is None


@pytest.mark.asyncio
async def test_restart_laeuft_im_thread_ohne_den_loop_zu_blockieren(audio):
    """Punkt 2 aus #25: der Wachhund ruft ueber ``asyncio.to_thread``.

    Hier nur die Vertragsseite — ``restart_input`` muss aus einem
    fremden Thread heraus funktionieren, sonst waere die Auslagerung
    im Wachhund unsicher.
    """
    audio.start_input()
    alt = audio.streams[0]

    await asyncio.to_thread(audio.restart_input)

    assert alt.geschlossen == 1
    assert len(audio.streams) == 2
