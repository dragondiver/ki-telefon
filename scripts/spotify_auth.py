"""Einmaliges Spotify-OAuth — erzeugt pro Konto ein Refresh-Token.

Bei Spotify Duo zweimal laufen lassen: einmal mit deinem Konto im Browser
eingeloggt, einmal mit dem deiner Frau. Jeder Lauf legt eine Token-Datei an
(z.B. token_florian.json), die das Telefon danach dauerhaft nutzt — kein
Browser-Login mehr nötig.

Ablauf:
  1. Spotify Developer Dashboard -> deine App -> Settings:
       Redirect URI eintragen:  http://127.0.0.1:8888/callback
       Client ID + Client Secret kopieren
  2. Beide Konten unter "User Management" als User eintragen
     (Name + E-Mail des jeweiligen Spotify-Kontos).
  3. .env ergänzen (siehe SPOTIFY_* unten), dann:

       python scripts/spotify_auth.py --account florian
       python scripts/spotify_auth.py --account anna

Headless Pi ohne Browser? Script auf dem Mac laufen lassen, die erzeugte
token_*.json danach auf den Pi kopieren — Connect-Steuerung ist kontoweit.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
PORT = int(urllib.parse.urlparse(REDIRECT_URI).port or 8888)

# Scopes: Wiedergabe steuern + Bibliothek/Playlists lesen.
SCOPES = " ".join(
    [
        "user-read-playback-state",
        "user-modify-playback-state",
        "playlist-read-private",
        "playlist-read-collaborative",
        "user-library-read",
        "user-follow-read",
    ]
)


def _pkce_pair() -> tuple[str, str]:
    """PKCE: Verifier + Challenge (sicherer, kein Secret im Auth-Request nötig)."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    code: str | None = None
    expected_state: str | None = None

    def do_GET(self) -> None:  # noqa: N802 (vorgegebener Name)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        received_state = params.get("state", [None])[0]
        # state muss zum selbst erzeugten Wert passen, sonst koennte ein
        # fremder Callback (z.B. ein woanders gestarteter Auth-Flow, an den
        # dieses lokale Skript verlinkt wird) seinen eigenen code einschleusen
        # und das Konto uebernehmen.
        if received_state != _CallbackHandler.expected_state:
            _CallbackHandler.code = None
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Ungueltiger state - Anfrage abgelehnt.</h2></body></html>"
            )
            return
        _CallbackHandler.code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = _CallbackHandler.code is not None
        msg = (
            "Spotify verbunden. Du kannst dieses Fenster schliessen."
            if ok
            else "Kein Code erhalten - bitte erneut versuchen."
        )
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *_args) -> None:
        pass  # Server-Logzeilen unterdruecken


def _wait_for_code(expected_state: str) -> str:
    _CallbackHandler.expected_state = expected_state
    server = http.server.HTTPServer(("127.0.0.1", PORT), _CallbackHandler)
    t = threading.Thread(target=server.handle_request)  # genau einen Request
    t.start()
    t.join(timeout=300)
    server.server_close()
    if _CallbackHandler.code is None:
        raise RuntimeError("Timeout/kein Code empfangen, oder state passte nicht (5 Min).")
    return _CallbackHandler.code


def authorize(account: str) -> None:
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET fehlen in .env")

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    url = f"{AUTH_URL}?{query}"
    print(f"\nKonto '{account}': im Browser einloggen ...")
    print("Falls sich nichts oeffnet, diese URL manuell aufrufen:\n")
    print(url, "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    code = _wait_for_code(state)

    # Code gegen Tokens tauschen (PKCE: verifier statt nur Secret).
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        auth=(client_id, client_secret),
        timeout=15,
    )
    resp.raise_for_status()
    tok = resp.json()

    out = Path(__file__).resolve().parents[1] / f"token_{account}.json"
    out.write_text(
        json.dumps(
            {
                "account": account,
                "refresh_token": tok["refresh_token"],
                "scope": tok.get("scope", SCOPES),
            },
            indent=2,
        )
    )
    # Langlebiges Refresh-Token: nicht world-readable auf einem Mehrbenutzer-Pi liegen lassen.
    out.chmod(0o600)
    print(f"OK -> {out}")
    print("Dieses Token ist langlebig; das Telefon erneuert Access-Tokens selbst.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Spotify-Refresh-Token pro Konto holen.")
    ap.add_argument("--account", required=True, help="Kurzname, z.B. florian / anna")
    authorize(ap.parse_args().account)
