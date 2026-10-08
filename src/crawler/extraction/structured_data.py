"""Structured-data strategies for HTML pages: JSON-LD and embedded JSON blobs.

The live Ouedkniss pages currently ship none of these (see docs/PARSING.md); the helpers exist
so that, if the site starts server-rendering listings, parsing keeps working and is testable.
"""

from __future__ import annotations

import re
from typing import Any

import orjson
from bs4 import BeautifulSoup

_ASSIGN_RE = re.compile(r"window\.__[A-Z_]+__\s*=\s*(\{.*?\})\s*;?\s*$", re.S)


def extract_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """All JSON-LD objects (flattening @graph and top-level arrays); malformed blocks skipped."""
    found: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            if "@graph" in node:
                visit(node["@graph"])
            found.append(node)

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            visit(orjson.loads(str(tag.string or tag.get_text() or "")))
        except orjson.JSONDecodeError:
            continue
    return found


def find_type(objects: list[dict[str, Any]], *types: str) -> dict[str, Any] | None:
    wanted = {t.lower() for t in types}
    for obj in objects:
        t = obj.get("@type")
        values = t if isinstance(t, list) else [t]
        if any(isinstance(v, str) and v.lower() in wanted for v in values):
            return obj
    return None


def extract_embedded_json(soup: BeautifulSoup) -> list[Any]:
    """JSON from <script type="application/json"> (e.g. __NEXT_DATA__) and
    `window.__SOMETHING__ = {...}` assignments."""
    blobs: list[Any] = []
    for tag in soup.find_all("script"):
        text = str(tag.string or tag.get_text() or "").strip()
        if not text:
            continue
        if tag.get("type") == "application/json":
            try:
                blobs.append(orjson.loads(text))
            except orjson.JSONDecodeError:
                pass
        elif text.startswith("window.__"):
            m = _ASSIGN_RE.match(text)
            if m:
                try:
                    blobs.append(orjson.loads(m.group(1)))
                except orjson.JSONDecodeError:
                    pass
    return blobs


def find_announcement(node: Any) -> dict[str, Any] | None:
    """Depth-first search for an object shaped like an API announcement."""
    if isinstance(node, dict):
        if "id" in node and "title" in node and ("slug" in node or "price" in node):
            return node
        for value in node.values():
            hit = find_announcement(value)
            if hit:
                return hit
    elif isinstance(node, list):
        for value in node:
            hit = find_announcement(value)
            if hit:
                return hit
    return None
