from kitelefon.config import Settings


def test_sprachkurs_defaults(monkeypatch, ohne_kitelefon_env):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    s = Settings(_env_file=None)
    assert s.sprachkurs_model == "gpt-realtime-2.1-mini"
    assert s.sprachkurs_enqueue is False
    assert str(s.sprachkurs_snapshot_path).endswith("sprachkurs_kontext.txt")
    assert str(s.sprachkurs_spool_dir).endswith("sprachkurs_spool")


def test_sprachkurs_paket_ist_importierbar():
    import sprachkurs.prompt  # noqa: F401
    from sprachkurs.regeln import vad_fuer_level

    assert vad_fuer_level(1) == 3000
