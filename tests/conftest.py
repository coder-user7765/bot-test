import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://www.ouedkniss.com"


@pytest.fixture
def load_json():
    return lambda name: json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def load_text():
    return lambda name: (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def base_url():
    return BASE


@pytest.fixture
def db(tmp_path):
    from crawler.storage.database import Database

    database = Database(tmp_path / "test.db")
    yield database
    database.close()
