"""Tests for the RFC 8941 Dictionary parser used to read back
Considerate-Agent headers. Includes a few cases adapted from the
httpwg/structured-field-tests suite (dictionary.json) restricted to the
subset this parser supports (bare items, no inner lists).
"""

import pytest

from considerate._structured_fields import parse_sf_dictionary


def test_basic_string_dictionary():
    assert parse_sf_dictionary('a="1", b="2"') == {"a": "1", "b": "2"}


def test_single_member():
    assert parse_sf_dictionary('a="foo"') == {"a": "foo"}


def test_empty_dictionary():
    assert parse_sf_dictionary("") == {}


def test_bare_key_is_boolean_true_shorthand():
    # RFC 8941 §3.2: "a member with a Boolean value of ?1 ... MAY omit that
    # value" — `a` alone means `a=?1`.
    assert parse_sf_dictionary("a, b=?0") == {"a": True, "b": False}


def test_escaped_quote_and_backslash_in_string():
    assert parse_sf_dictionary(r'a="a \"quoted\" value"') == {"a": 'a "quoted" value'}
    assert parse_sf_dictionary(r'a="back\\slash"') == {"a": "back\\slash"}


def test_integer_and_decimal_values():
    result = parse_sf_dictionary("count=42, ratio=1.5, neg=-3")
    assert result == {"count": 42, "ratio": 1.5, "neg": -3}


def test_token_value():
    assert parse_sf_dictionary("mode=fetcher") == {"mode": "fetcher"}


def test_byte_sequence_value():
    import base64

    encoded = base64.b64encode(b"hi").decode()
    assert parse_sf_dictionary(f"data=:{encoded}:") == {"data": b"hi"}


def test_parameters_are_skipped_not_fatal():
    # `;expires=...` is valid SF syntax this parser deliberately doesn't
    # surface (see module docstring) — it must not blow up parsing either.
    assert parse_sf_dictionary('a="x";expires=1234567') == {"a": "x"}


@pytest.mark.parametrize(
    "value",
    [
        'a="unterminated',
        "a=",
        ",",
        "a=,b=1",
        "1abc=2",  # keys can't start with a digit
    ],
)
def test_malformed_input_raises(value):
    with pytest.raises(ValueError):
        parse_sf_dictionary(value)
