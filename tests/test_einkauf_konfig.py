from kitelefon.tools import handle_add_shopping_item


async def test_ohne_url_meldet_nicht_konfiguriert():
    ergebnis = await handle_add_shopping_item({"item": "Milch"}, "", "geheim")
    assert ergebnis == {"ok": False, "error": "mise-en-place not configured"}


async def test_ohne_secret_meldet_nicht_konfiguriert():
    ergebnis = await handle_add_shopping_item({"item": "Milch"}, "https://example.invalid", "")
    assert ergebnis == {"ok": False, "error": "mise-en-place not configured"}
