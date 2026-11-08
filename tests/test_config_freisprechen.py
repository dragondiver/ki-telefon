"""Freisprechen (#38) ist ohne Konfiguration aus — beide Werte leer."""

from kitelefon.config import Settings


def test_freisprechen_defaults_leer(monkeypatch, ohne_kitelefon_env):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    s = Settings(_env_file=None)
    assert s.rundfunk_verleih_url == ""
    assert s.rundfunk_agent_token == ""


def test_freisprechen_aus_der_umgebung(monkeypatch, ohne_kitelefon_env):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("KITELEFON_RUNDFUNK_VERLEIH_URL", "ws://bridge:8000/verleih")
    monkeypatch.setenv("KITELEFON_RUNDFUNK_AGENT_TOKEN", "t")
    s = Settings(_env_file=None)
    assert s.rundfunk_verleih_url == "ws://bridge:8000/verleih"
    assert s.rundfunk_agent_token == "t"
