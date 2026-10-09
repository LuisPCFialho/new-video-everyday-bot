import pytest

from reels_bot.captions import MAX_CAPTION_CHARS, caption_errors, count_hashtags, first_line, normalize, parse_captions

SAMPLE = """﻿01 · What if the 1755 earthquake hit Lisbon today?

On 1 November 1755, Lisbon was hit by an earthquake — all in one day.
👀 Did you spot the tourist?

#whatif #lisbon #lisboa

========================================

02 · What if Vesuvius erupted today?

Vesuvius buried Pompeii in AD 79. 🌋

#whatif #vesuvius
========
14 - What if you were on the Titanic?
April 14, 1912, 11:40 pm.
#titanic #1912
"""


def test_parse_real_format():
    entries = parse_captions(SAMPLE)
    assert [e.number for e in entries] == [1, 2, 14]
    assert entries[0].title == "What if the 1755 earthquake hit Lisbon today?"
    assert entries[0].body.startswith("On 1 November 1755")
    assert entries[0].body.endswith("#whatif #lisbon #lisboa")
    assert "01 ·" not in entries[0].body
    assert entries[2].title == "What if you were on the Titanic?"


def test_parse_rejects_block_without_header():
    with pytest.raises(ValueError, match="cabeçalho"):
        parse_captions("Sem número\ntexto\n========\n02 · T\nx")


def test_parse_rejects_duplicate_numbers():
    with pytest.raises(ValueError, match="repetido"):
        parse_captions("01 · A\nx\n========\n01 · B\ny")


def test_parse_rejects_empty_body():
    with pytest.raises(ValueError, match="vazia"):
        parse_captions("01 · A\n\n========\n02 · B\ny")


def test_hashtags_counted_but_not_urls_or_entities():
    assert count_hashtags("#a #b_c #1912 texto#nao http://x.com/#frag &#123;") == 3


def test_caption_errors():
    assert caption_errors("ok #a") == []
    assert "vazia" in caption_errors("   ")[0]
    assert "caracteres" in caption_errors("x" * (MAX_CAPTION_CHARS + 1))[0]
    assert "31 hashtags" in caption_errors(" ".join(f"#t{i}" for i in range(31)))[0]


def test_first_line_and_normalize():
    assert first_line("\n\n  Olá, mundo! 🌍 \nresto") == "Olá, mundo! 🌍"
    assert first_line(None) == ""
    assert normalize("On 1 November 1755, Lisbon was hit — 🌊🔥!") == "on 1 november 1755 lisbon was hit"
    assert normalize("WHAT IF... Lisbon?!") == normalize("what if lisbon")
    assert normalize(None) == ""
