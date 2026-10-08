from __future__ import annotations

from pydantic import BaseModel


class Image(BaseModel):
    url: str
    position: int = 0
    alt: str | None = None
    width: int | None = None
    height: int | None = None
    source: str = "listing"
