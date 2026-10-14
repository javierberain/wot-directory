#!/usr/bin/env python3
"""Tests for directory_rules.normalize_age / refine_age (age vocabulary).
Runs standalone or under pytest."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
from directory_rules import normalize_age, refine_age, AGE_BANDS  # noqa: E402


def test_bands_and_numbers():
    cases = {
        "middle years": "middle years", "Middle years": "middle years", "middle-aged": "middle years",
        "old; much older than the other students": "older", "Older (gray-haired)": "older", "aging": "older",
        "elderly (pensioner)": "elderly", "ancient, imprisoned for three thousand years": "elderly",
        "young": "young", "young man": "young", "youngish": "young", "youngest of the four Wisdoms": "young",
        "young woman, old enough to dance the Spring Pole": "young", "in her late twenties": "young",
        "girl": "child", "Five years old": "child", "Nine": "child", "Adult": "adult",
        "ageless Aes Sedai features": "ageless",
        "fourteen": "about 14", "nineteen": "about 19", "Nearly twenty": "about 20", "Over seventy": "about 70",
        "sixty-three": "about 63", "about 40": "about 40", "older than Mat (ca. 30)": "about 30",
    }
    bad = {k: (v, normalize_age(k)) for k, v in cases.items() if normalize_age(k) != v}
    assert not bad, bad


def test_non_ages_and_comparatives_drop():
    for s in ["alive", "deceased", "deceased, killed twenty years ago", "Unknown", "long dead (historical)",
              "older than Aviendha", "same age as Perrin", "Of an age with Edorion", "not much older than Rand",
              "Has been a peddler for twenty years", "Aes Sedai for 130 years", "over forty years as a Darkfriend", ""]:
        assert normalize_age(s) is None, s


def test_refine_is_forward_only():
    assert refine_age("young", "grown man") == "adult"
    assert refine_age("older", "young lad") == "older"
    assert refine_age("young", "19 years old") == "about 19"      # same band, number is more specific
    assert refine_age("about 19", "young") == "about 19"
    assert refine_age("adult", "ageless") == "ageless"
    assert refine_age("ageless", "young novice") == "ageless"
    assert refine_age(None, "alive") is None
    assert refine_age("middle years", "elderly") == "elderly"
    assert [refine_age(a, b) for a, b in zip(AGE_BANDS, AGE_BANDS[1:])] == AGE_BANDS[1:]


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"ok    {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1; print(f"FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
