from bs4 import BeautifulSoup

from crawler.extraction.image_parser import build_images, parse_api_media, parse_html_images
from crawler.utils.urls import normalize_image_url


def test_no_images():
    assert parse_api_media([], None) == []
    assert parse_api_media(None, None) == []


def test_default_media_used_when_no_medias():
    imgs = parse_api_media([], {"mediaUrl": "https://cdn7.ouedkniss.com/a.jpg", "mimeType": "image/jpeg"})
    assert [i.url for i in imgs] == ["https://cdn7.ouedkniss.com/a.jpg"]


def test_multiple_images_ordered_deduped_non_images_skipped():
    medias = [{"mediaUrl": f"https://c/{n}.jpg", "mimeType": "image/jpeg"} for n in "abca"]
    medias.append({"mediaUrl": "https://c/v.mp4", "mimeType": "video/mp4"})
    imgs = parse_api_media(medias)
    assert [(i.url, i.position) for i in imgs] == [("https://c/a.jpg", 0), ("https://c/b.jpg", 1), ("https://c/c.jpg", 2)]


def test_url_normalisation():
    assert normalize_image_url("//CDN7.ouedkniss.com/x.jpg#frag") == "https://cdn7.ouedkniss.com/x.jpg"
    assert normalize_image_url("http://a.com/x.jpg?w=1") == "https://a.com/x.jpg?w=1"
    assert normalize_image_url("/img/x.jpg", "https://www.ouedkniss.com/p/1") == "https://www.ouedkniss.com/img/x.jpg"
    assert normalize_image_url("data:image/png;base64,AAA") is None
    assert normalize_image_url("  ") is None and normalize_image_url(None) is None


def test_html_images_with_dimensions():
    soup = BeautifulSoup('<img src="/a.jpg" alt=" Front " width="640" height="480px"><img data-src="/b.jpg">', "lxml")
    imgs = parse_html_images(soup, "https://www.ouedkniss.com")
    assert imgs[0].alt == "Front" and (imgs[0].width, imgs[0].height) == (640, 480)
    assert imgs[1].url == "https://www.ouedkniss.com/b.jpg" and imgs[1].alt is None


def test_build_images_empty():
    assert build_images([]) == []
