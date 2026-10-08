from crawler.discovery.listings import to_discovered
from crawler.models.listing import Listing, Price
from crawler.normalization.listing_normalizer import normalize_listing
from crawler.utils.urls import extract_external_id, listing_url, normalize_url


def make(ext_id, title="Phone", desc="Nice phone", slug=None, **kw):
    l = Listing(external_id=ext_id, url=f"https://www.ouedkniss.com/{slug or 'phone'}-d{ext_id}",
                title=title, description=desc, price=Price(amount=100, currency="DZD", raw="100 UNIT"), **kw)
    return normalize_listing(l)


def seed(db, *listings):
    items = to_discovered([{"id": l.external_id, "slug": l.url.rsplit("/", 1)[1].rsplit("-d", 1)[0]} for l in listings],
                          "https://www.ouedkniss.com")
    db.add_discovered(items, "cat", ("cursor:cat", "2"))


# --------- URL normalisation ---------
def test_url_normalisation():
    assert normalize_url("HTTP://WWW.Ouedkniss.com/abc-d1/?utm_source=x&b=2&a=1#top") == \
        "https://www.ouedkniss.com/abc-d1?a=1&b=2"
    assert normalize_url("https://www.ouedkniss.com//abc-d1//") == "https://www.ouedkniss.com/abc-d1"
    assert listing_url("https://www.ouedkniss.com/", "x-y", "9") == "https://www.ouedkniss.com/x-y-d9"
    assert extract_external_id("https://www.ouedkniss.com/some-slug-d12345") == "12345"
    assert extract_external_id("https://www.ouedkniss.com/informatique/2") is None


# --------- discovery-level dedupe ---------
def test_discovery_dedupes_by_external_id(db):
    nodes = [{"id": 7, "slug": "a"}, {"id": "7", "slug": "a-renamed"}, {"id": 8}, {"slug": "no-id"}]
    items = to_discovered(nodes, "https://www.ouedkniss.com")
    assert [i.external_id for i in items] == ["7"]
    assert db.add_discovered(items, "cat", ("cursor:cat", "2")) == 1
    renamed = to_discovered([{"id": "7", "slug": "other-slug"}], "https://www.ouedkniss.com")
    assert db.add_discovered(renamed, "cat") == 0           # same id, different slug: not a new record
    assert db.counts()["discovered"] == 1


# --------- storage-level dedupe ---------
def test_same_id_reprocessed_is_unchanged_then_updated(db):
    a = make("1"); seed(db, a)
    assert db.save_listing(a) == "inserted"
    assert db.save_listing(make("1")) == "unchanged"
    assert db.save_listing(make("1", desc="edited text")) == "updated"
    assert db.counts()["listings"] == 1


def test_same_content_different_id_is_duplicate(db):
    a, b = make("1"), make("2"); seed(db, a, b)
    assert db.save_listing(a) == "inserted"
    assert db.save_listing(b) == "duplicate"
    c = db.counts()
    assert c["listings"] == 1 and c["duplicates"] == 1 and c["skipped"] == 1 and c["success"] == 1


def test_title_alone_is_never_a_duplicate_key(db):
    a, b = make("1", desc="first"), make("2", desc="second"); seed(db, a, b)
    assert db.save_listing(a) == "inserted" and db.save_listing(b) == "inserted"
    assert db.counts()["listings"] == 2


def test_same_url_with_new_id_treated_as_same_record(db):
    a = make("1"); seed(db, a)
    db.save_listing(a)
    clone = make("99"); clone.url = a.url
    assert db.save_listing(clone) in ("unchanged", "updated")
    assert db.counts()["listings"] == 1


def test_failure_then_success_clears_failed_table(db):
    a = make("1"); seed(db, a)
    db.mark_processing(a.url)
    db.mark_failed(a.url, "boom", 500)
    assert db.counts()["failed"] == 1 and len(db.failed_rows()) == 1
    assert db.requeue_failed() == 1
    db.save_listing(a)
    assert db.counts()["failed"] == 0 and db.failed_rows() == []


def test_interrupted_processing_rows_are_requeued(db):
    a = make("1"); seed(db, a)
    db.mark_processing(a.url)
    assert db.counts()["pending"] == 1 and db.pending(10) == []
    assert db.recover_interrupted() == 1
    assert len(db.pending(10)) == 1
