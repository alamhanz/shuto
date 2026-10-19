"""places.resolve: the place decision, its labels and the GPS/file merge."""

from __future__ import annotations

import itertools

from shuto.places import geo
from shuto.places.resolve import (
    PLACE_SOURCES,
    default_flag,
    merge_known_place,
    resolve_place,
    short_place_text,
)

CITY = {"city": "Kyoto", "country": "Japan"}
KNOWNS = (
    None,
    {"city": "Shibuya", "country": "Japan", "source": "gps"},
    {"city": "Köln", "country": "Germany", "source": "file"},
    {"region": "Tokyo", "country": "Japan", "source": "gps"},
    {"country": "France", "source": "file"},
)


def test_resolve_place_only_returns_known_sources():
    locations = (None, {}, CITY, {"city": "Osaka"}, {"country": "Japan"})
    defaults = (("", ""), ("Tokyo", "Japan"), ("Paris", "France"))
    for loc, known, (city, country) in itertools.product(locations, KNOWNS, defaults):
        place = resolve_place(loc, city, country, known)
        assert place["source"] in PLACE_SOURCES
        assert set(place) == {"city", "region", "country", "source", "default"}


def test_short_place_text_and_default_flag():
    assert short_place_text({"city": "TOKYO", "country": "JAPAN"}) == "TOKYO, JAPAN"
    assert short_place_text({"country": "JAPAN"}) == "JAPAN"
    assert short_place_text(None) == "" and short_place_text("TOKYO") == ""
    assert default_flag({"default": True}) == "yes"
    assert default_flag({"default": False}) == "no"
    assert default_flag(None) == ""


def test_merge_known_place():
    gps = {"city": None, "region": "Tokyo", "country": "Japan", "neighborhood": "Hood"}
    file = {"city": "Shibuya", "region": None, "country": None}
    merged = merge_known_place(dict(gps), file)
    assert merged["city"] == "Shibuya" and merged["region"] == "Tokyo"
    assert merge_known_place(None, file) is file
    assert merge_known_place(None, None) is None
    assert merge_known_place(dict(gps, city="Ueno"), file)["city"] == "Ueno"
    assert merge_known_place(dict(gps), file, neighborhood=False)["neighborhood"] is None


def test_geo_clear_forgets_the_index():
    geo._index = geo._Index("")
    geo.clear()
    assert geo._index is None


def test_resolve_place_region_for_the_caption():
    ai = {"city": "Kamakura", "region": "Kanagawa", "country": "Japan"}
    assert resolve_place(ai)["region"] == "KANAGAWA"
    known = {"city": "Shibuya", "region": "Tokyo", "country": "Japan", "source": "gps"}
    assert resolve_place(ai, known=known)["region"] == "TOKYO"  # the photo's own wins
    same = {"city": "Tokyo", "region": "Tokyo", "country": "Japan"}
    assert resolve_place(same)["region"] == ""  # never a repeat of the city
    assert resolve_place({"region": "Osaka"}, "Kyoto", "Japan")["region"] == ""  # default city
    only_region = {"region": "Tokyo", "source": "file"}
    got = resolve_place({}, known=only_region)
    assert (got["city"], got["region"]) == ("TOKYO", "")  # the region stood in for the city
