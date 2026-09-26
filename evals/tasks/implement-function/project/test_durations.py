import pytest

from durations import parse_duration


def test_examples():
    assert parse_duration("45s") == 45
    assert parse_duration("1h30m") == 5400
    assert parse_duration("2d4h30m") == 189_000


def test_single_units():
    assert parse_duration("7d") == 604_800
    assert parse_duration("12m") == 720


def test_invalid():
    for bad in ["", "hm", "10x", "3m2h", "1h1h", "abc"]:
        with pytest.raises(ValueError):
            parse_duration(bad)
