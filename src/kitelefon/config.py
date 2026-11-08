from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="KITELEFON_", extra="ignore")

    platform: Literal["mac", "pi"] = "mac"
    db_path: Path = Path("tasks.db")
    # Delegationen wandern nach Railway-Postgres; ohne URL bleibt alles lokal
    # in tasks.db (Entwicklung, Notbetrieb).
    tasks_database_url: str | None = None
    log_level: str = "INFO"
    openai_api_key: str = Field(..., validation_alias="OPENAI_API_KEY")
    openai_model: str = "gpt-realtime-2.1-mini"
    half_duplex: bool = (
        True  # suppress mic while speaker plays; set False on Pi (handset provides isolation)
    )
    record_calls: bool = False  # WAV-Mitschnitt jeder KI-Antwort nach recordings/
    # Gedaechtnis pro Rolle: Fakten dauerhaft, letztes Gespraech 14 Tage.
    # Getrennt je Figur — was Hein weiss, weiss Marlene nicht. Kostet nach
    # jedem Anruf einen Claude-Aufruf, deshalb bewusst abschaltbar.
    role_memory: bool = False
    # Server-VAD. Gratwanderung zwischen Fehl-Barge-in (zu niedrig) und
    # nicht erkannten leisen/tiefen Stimmen (zu hoch). Am Geraet abstimmen.
    vad_threshold: float = 0.6
    vad_prefix_padding_ms: int = 500
    # War 500ms, brach am 2026-08-12 bei nachdenklichem Sprechen mit
    # Denkpausen ("Kannst du... äh... raussuchen") in bis zu vier Fragmente
    # auseinander, das Modell antwortete auf jedes einzelne. 800ms als Test
    # (docs/befunde-2026-08-12-gespraechsanalyse.md, Befund 2) — reagiert
    # dafür etwas träger.
    vad_silence_ms: int = 800
    bell_enabled: bool = True  # Klingel ansteuern (auf dem Mac nur Konsolen-Ausgabe)
    startup_ring: bool = True  # beim Start einmal klingeln als Funktionstest
    autocall_delay_s: float = 0.0  # Sekunden Ruhe bis die KI selbst anruft; 0 = aus
    autocall_max_rings: int = 5
    # Ziffer 0 fährt den Pi herunter (Auflegen während der Gnadenfrist bricht ab).
    # Braucht passwortloses sudo, siehe README → "Herunterfahren per Wählscheibe".
    shutdown_digit_enabled: bool = False
    shutdown_grace_s: float = 5.0
    shutdown_command: str = "sudo shutdown -h now"
    # Rueckrufe des Heim-Agenten: er schickt fertige Ergebnisse hierher, das
    # Telefon klingelt und liest vor. Port 0 schaltet den Zuhoerer ab.
    # Ohne Token startet der Zuhoerer nur auf Loopback (127.0.0.1/localhost);
    # auf jedem anderen Host wird ein Token verlangt, sonst bleibt er aus.
    callback_port: int = 0
    callback_host: str = "0.0.0.0"
    callback_path: str = "/api/agent/callback"
    callback_token: str = ""
    mise_en_place_url: str = ""
    mise_en_place_secret: str = ""
    # Werkzeug-Dienst fuer andere Geraete (rundfunk/Elektra, rundfunk#31):
    # GET /werkzeuge, POST /werkzeuge/<name>. Port 0 = aus. Ohne Token nimmt
    # der Dienst nichts an — er fuehrt Werkzeuge mit unseren Geheimnissen aus.
    werkzeug_port: int = 0
    werkzeug_host: str = "0.0.0.0"
    werkzeug_token: str = ""
    # Websuche im Gespraech (web_suche): Obergrenze pro Kalendertag, damit eine
    # Schleife oder ein Missverstaendnis nicht teuer wird (rund 1 Cent je Suche).
    # 0 sperrt die Websuche.
    websuche_max_pro_tag: int = 50
    # Wohnort fuer "Wie ist das Wetter bei uns/hier?" (#141). Leer = keiner:
    # get_weather braucht dann wie bisher einen genannten Ort.
    wohnort: str = ""
    # Freisprechen ueber Elektra (#38): die rundfunk-Bridge verleiht das
    # Sprachgeraet als Netz-Audiogeraet (NetzAudioIO). URL des Verleih-
    # Endpunkts, z.B. ws://agent-pi.local:8000/verleih, und das Token,
    # das dort als RUNDFUNK_AGENT_TOKEN steht. Beides leer = kein Freisprechen.
    rundfunk_verleih_url: str = ""
    rundfunk_agent_token: str = ""
    # Spotify-Settings werden direkt aus os.environ in agents/spotify.py gelesen
    # (SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI,
    #  SPOTIFY_DEVICE_NAME, SPOTIFY_ACCOUNT_1, SPOTIFY_ACCOUNT_2)

    # --- Franzoesischkurs (Rolle 3, Spec ../sprachkurs) ---
    # Modell nur fuer Camille, getrennt von openai_model einstellbar.
    # Default wie fuer alle Rollen gpt-realtime-2.1-mini (#131).
    sprachkurs_model: str = "gpt-realtime-2.1-mini"
    # Name der Lernenden, wie er in der sprachkurs-Datenbank steht; zugleich
    # Empfaenger der Auswertungsmail. Steht nur in der .env (#149). Leer =
    # Camille meldet "Kurs nicht erreichbar", statt still zu scheitern.
    sprachkurs_lernende: str = ""
    # Auswertungs-Task in die Remote-Queue stellen. Bleibt aus, bis
    # Plan 3 die konsument-Spalte und die Filter im heim-agent liefert —
    # vorher wuerde dessen Worker unsere Tasks wegschnappen.
    sprachkurs_enqueue: bool = False
    # Offline-Netz (Spec §6): letzter Kontextblock als Datei, damit ein
    # Netzausfall beim Abheben nicht die Fehleransage bedeutet.
    sprachkurs_snapshot_path: Path = Path("sprachkurs_kontext.txt")
    sprachkurs_spool_dir: Path = Path("sprachkurs_spool")


def load_settings() -> Settings:
    return Settings()
