from __future__ import annotations

import hashlib
from typing import Any

import orjson


def content_hash(payload: Any) -> str:
    """Stable SHA-256 of any JSON-serialisable value (key order does not matter)."""
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
