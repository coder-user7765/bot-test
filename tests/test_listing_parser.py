from crawler.extraction.listing_parser import parse_api_announcement, parse_html_listing
from crawler.normalization.listing_normalizer import normalize_listing


def parse(data, base_url):
    return normalize_listing(parse_api_announcement(data, base_url))


def test_normal_french_listing(load_json, base_url):
    l = parse(load_json("api_detail_fr.json"), base_url)
    assert l.source == "ouedkniss" and l.external_id == "1001"
    assert l.url == f"{base_url}/smartphones-samsung-galaxy-s24-ultra-256-go-neuf-oran-algerie-d1001"
    assert l.title == "Samsung Galaxy S24 Ultra 256 Go neuf"          # whitespace collapsed
    assert l.category == "Téléphones & Accessoires" and l.subcategory == "Smartphones"
    assert (l.price.amount, l.price.currency, l.price.raw, l.price.unit) == (185000, "DZD", "185000 UNIT", "UNIT")
    assert (l.location.wilaya, l.location.commune, l.location.raw) == ("Oran", "Bir el djir", "Bir el djir, Oran")
    assert "Livraison possible vers 58 wilayas" in l.description      # NBSP -> space
    assert "\n\n\n" not in l.description
    assert l.published_at == "2026-04-14T11:12:53Z"
    assert l.seller.display_name == "TelShop Oran" and l.seller.type == "store"
    assert l.attributes == {"marque": "Samsung", "stockage": "256 Go", "etat": "Etat neuf"}  # empty spec dropped
    assert l.source_metadata["attribute_labels"]["marque"] == "Marque"
    assert l.source_metadata["refreshed_at"] == "2026-10-08T05:30:08.000Z"
    assert l.content_hash and len(l.content_hash) == 64


def test_arabic_listing_preserved(load_json, base_url):
    l = parse(load_json("api_detail_ar.json"), base_url)
    assert l.title == "شقة F3 للبيع في حيدرة"
    assert l.location.commune == "حيدرة" and l.location.wilaya == "الجزائر"
    assert l.location.raw == "شارع الاستقلال, حيدرة, الجزائر"
    assert l.seller.display_name == "محمد" and l.seller.type == "individual"
    assert l.category == "Immobilier" and l.subcategory == "Appartement"
    assert l.price.amount == 18000000 and l.price.raw == "1.8 BILLION"
    assert l.attributes == {"surface": 95}
    dumped = l.model_dump_json()
    assert "شقة" in dumped                      # not \u-escaped in the model JSON either


def test_missing_price_description_images_and_malformed_location(load_json, base_url):
    l = parse(load_json("api_detail_minimal.json"), base_url)
    assert l.title == "Vends table"
    assert l.price.amount is None and l.price.currency is None and l.price.raw is None
    assert l.description is None and l.published_at is None
    assert l.images == []
    assert l.seller.display_name is None and l.seller.type is None
    # region missing: only what is known is stored, nothing guessed
    assert l.location.commune == "Blida" and l.location.wilaya is None and l.location.raw == "Blida"
    assert l.category == "Meubles" and l.subcategory is None


def test_multiple_images_dedup_and_videos_skipped(load_json, base_url):
    l = parse(load_json("api_detail_fr.json"), base_url)
    assert [i.position for i in l.images] == [0, 1, 2]
    assert [i.url.rsplit("/", 1)[1] for i in l.images] == ["a.jpg", "b.jpg", "c.jpg"]
    assert all(i.source == "listing" and i.width is None for i in l.images)


def test_search_summary_record(load_json, base_url):
    l = parse(load_json("api_search_summary.json"), base_url)
    assert l.price.amount == 2300000 and l.price.raw == "230 MILLION"
    assert l.attributes == {"annee": "2021"}
    assert len(l.images) == 1 and l.source_metadata["is_detail_record"] is False


def test_html_json_ld_strategy(load_text, base_url):
    l = normalize_listing(parse_html_listing(load_text("listing_jsonld.html"), "https://www.ouedkniss.com/x", base_url))
    assert l.external_id == "5005"
    assert l.title == "Table en bois massif" and l.category == "Meubles"
    assert l.price.amount == 45000 and l.price.currency == "DZD"
    assert l.seller.display_name == "Karim"
    assert [i.url for i in l.images] == ["https://cdn7.ouedkniss.com/1600/a.jpg", "https://cdn8.ouedkniss.com/1600/b.jpg"]


def test_html_semantic_and_css_strategies(load_text, base_url):
    l = normalize_listing(parse_html_listing(load_text("listing_plain.html"), "https://www.ouedkniss.com/foo", base_url))
    assert l.external_id == "6006" and l.url.endswith("-d6006")   # tracking param dropped
    assert l.title == "سيارة رونو كليو 4"
    assert l.price.amount == 450000 and l.price.currency == "DZD" and l.price.raw == "450 000 DA"
    assert l.published_at == "2026-10-01T09:00:00Z"
    urls = [i.url for i in l.images]
    assert urls[0] == "https://www.ouedkniss.com/img/og.jpg" and len(urls) == 2   # data: URI ignored
    assert l.images[1].alt == "front" and l.images[1].width == 800


def test_html_without_listing_returns_none(load_text, base_url):
    assert parse_html_listing(load_text("listing_nothing.html"), "https://www.ouedkniss.com/", base_url) is None
