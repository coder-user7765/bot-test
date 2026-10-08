import pytest

from crawler.models.listing import Listing, Location, Price
from crawler.normalization.listing_normalizer import (clean_text, normalize_datetime,
                                                      normalize_listing, parse_price_text)


@pytest.mark.parametrize("raw,amount,currency", [
    ("450 000 DA", 450000, "DZD"),
    ("450000 DA", 450000, "DZD"),
    ("450,000 DA", 450000, "DZD"),
    ("450.000 DA", 450000, "DZD"),
    ("450000 DZD", 450000, "DZD"),
    ("450 000 DA", 450000, "DZD"),
    ("1 250 000,50 DA", 1250000.5, "DZD"),
    ("450000", 450000, None),               # currency not stated -> not invented
    ("1,5 Millions", 15000, None),         # Algerian "million" = 10,000 DA
    ("2 Milliards DA", 20000000, "DZD"),
])
def test_price_formats(raw, amount, currency):
    p = parse_price_text(raw)
    assert p.amount == amount and p.currency == currency and p.raw == raw   # raw is never altered


@pytest.mark.parametrize("raw", ["", None, "   "])
def test_empty_price(raw):
    assert parse_price_text(raw) == Price()


@pytest.mark.parametrize("raw", ["à débattre", "N/A", "DA", "--"])
def test_malformed_price_keeps_raw_only(raw):
    p = parse_price_text(raw)
    assert p.amount is None and p.currency is None and p.raw == raw


def test_normalizer_fills_price_from_raw_but_keeps_raw():
    l = normalize_listing(Listing(external_id="1", url="https://x/a-d1", price=Price(raw="450 000 DA"), title="t"))
    assert l.price.amount == 450000 and l.price.currency == "DZD" and l.price.raw == "450 000 DA"


def test_zero_price_means_unknown():
    l = normalize_listing(Listing(external_id="1", url="u", price=Price(amount=0, currency="DZD", raw="0 UNIT"), title="t"))
    assert l.price.amount is None and l.price.currency is None and l.price.raw == "0 UNIT"


def test_location_only_one_part_known():
    l = normalize_listing(Listing(external_id="1", url="u", title="t", location=Location(wilaya="Oran")))
    assert l.location.wilaya == "Oran" and l.location.commune is None and l.location.raw == "Oran"


def test_malformed_location_is_stripped_not_guessed():
    l = normalize_listing(Listing(external_id="1", url="u", title="t", location=Location(raw="  ​ Oran  ,   Es Senia ")))
    assert l.location.raw == "Oran , Es Senia"
    assert l.location.wilaya is None and l.location.commune is None


def test_clean_text():
    assert clean_text("  a ​ b c\x00 ") == "a b c"
    assert clean_text("\n\n") is None and clean_text(None) is None
    assert clean_text("a\r\n\r\n\r\n\r\nb  c", keep_newlines=True) == "a\n\nb c"
    assert clean_text("é") == "é"        # NFC


def test_datetime_normalisation():
    assert normalize_datetime("2026-04-14T11:12:53.227409Z") == "2026-04-14T11:12:53Z"
    assert normalize_datetime("2026-10-01T10:00:00+01:00") == "2026-10-01T09:00:00Z"
    assert normalize_datetime("not a date") is None and normalize_datetime(None) is None


def test_hash_is_stable_and_ignores_id_url_and_scrape_time():
    a = normalize_listing(Listing(external_id="1", url="u1", title="Same", description="d", scraped_at="2026-01-01T00:00:00Z"))
    b = normalize_listing(Listing(external_id="2", url="u2", title="Same", description="d", scraped_at="2027-01-01T00:00:00Z"))
    c = normalize_listing(Listing(external_id="3", url="u3", title="Same", description="different"))
    assert a.content_hash == b.content_hash != c.content_hash


def test_no_hash_without_content():
    assert normalize_listing(Listing(external_id="1", url="u")).content_hash is None
