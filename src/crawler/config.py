"""Configuration: config/crawler.yaml  <  .env / environment  <  CLI flags."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Desktop Chrome on Windows. Override with `user_agent` in config/crawler.yaml or USER_AGENT in .env.
BROWSER_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Friendly environment-variable aliases -> config field names.
ENV_ALIASES = {
    "MAX_CONCURRENCY": "max_concurrent_requests",
    "REQUEST_DELAY": "request_delay_seconds",
    "SAVE_RAW_HTML": "save_raw",
}


class CrawlerConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    base_url: str = "https://www.ouedkniss.com"
    api_url: str = "https://api.ouedkniss.com/graphql"

    # limits per invocation (0 = unlimited)
    max_listings: int = Field(100, ge=0)
    max_pages: int = Field(20, ge=0)
    max_requests: int = Field(0, ge=0)

    # politeness
    request_delay_seconds: float = Field(2.0, ge=0)
    request_jitter_seconds: float = Field(1.0, ge=0)
    max_concurrent_requests: int = Field(2, ge=1, le=4)
    timeout_seconds: float = Field(30.0, gt=0)
    retry_count: int = Field(3, ge=0, le=10)
    backoff_base_seconds: float = Field(5.0, ge=0)
    backoff_max_seconds: float = Field(300.0, ge=0)
    stop_on_block: bool = True
    respect_robots: bool = True
    user_agent: str = BROWSER_USER_AGENT
    accept_language: str = "fr-FR,fr;q=0.9,ar;q=0.8,en;q=0.7"
    browser_enabled: bool = True
    browser_channel: str = "chrome"
    browser_headless: bool = False
    max_block_events: int = Field(3, ge=1)  # consecutive 429s / 403s before stopping
    max_connection_failures: int = Field(6, ge=1)  # consecutive transport errors before stopping
    max_url_attempts: int = Field(3, ge=1)  # per-listing attempts before it is marked FAILED
    cooldown_seconds: float = Field(900.0, ge=0)  # only used when stop_on_block is false

    # what to crawl
    categories: list[str] = Field(default_factory=list)
    category_depth: int = Field(1, ge=1, le=2)
    page_size: int = Field(48, ge=1, le=100)
    max_pages_per_category: int = Field(0, ge=0)
    fetch_details: bool = True

    # output
    save_raw: bool = Field(False, validation_alias=AliasChoices("save_raw", "save_raw_html"))
    download_images: bool = False
    data_dir: str = "data"

    # resolved at load time (not read from YAML)
    root: Path = Field(default=PROJECT_ROOT, exclude=True)

    # ---- derived paths -------------------------------------------------
    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self) -> Path:
        return self.data_path / "crawler.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_path / "raw"

    @property
    def normalized_dir(self) -> Path:
        return self.data_path / "normalized"

    @property
    def exports_dir(self) -> Path:
        return self.data_path / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.data_path / "logs"

    @property
    def images_dir(self) -> Path:
        return self.data_path / "images"

    def ensure_dirs(self) -> None:
        for p in (self.data_path, self.raw_dir, self.normalized_dir, self.exports_dir,
                  self.logs_dir, self.images_dir):
            p.mkdir(parents=True, exist_ok=True)


def default_config_path() -> Path:
    cwd_candidate = Path.cwd() / "config" / "crawler.yaml"
    return cwd_candidate if cwd_candidate.exists() else PROJECT_ROOT / "config" / "crawler.yaml"


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(path: Path | None = None, overrides: dict[str, Any] | None = None) -> CrawlerConfig:
    path = Path(path) if path else default_config_path()
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "save_raw_html" in data:  # YAML spelling from the spec; normalise before env overrides
        data["save_raw"] = data.pop("save_raw_html")
    root = path.resolve().parent.parent if path.exists() else PROJECT_ROOT

    env = {**_read_dotenv(root / ".env"), **os.environ}
    field_names = set(CrawlerConfig.model_fields) - {"root"}
    for key, value in env.items():
        name = ENV_ALIASES.get(key.upper(), key.lower())
        if name in field_names and value != "":
            if name == "categories":
                data[name] = [c.strip() for c in value.split(",") if c.strip()]
            else:
                data[name] = value

    for key, value in (overrides or {}).items():
        if value is not None:
            data[key] = value

    cfg = CrawlerConfig.model_validate(data)
    cfg.root = root
    return cfg
