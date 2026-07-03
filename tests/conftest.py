from pathlib import Path

import pytest

from ledger.config import Config, load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def config() -> Config:
    """The real config.toml — tests run against what will actually be deployed."""
    return load_config(PROJECT_ROOT / "config.toml")
