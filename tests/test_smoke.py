"""Smoke test: confirm pytest collects and runs, and that src.config imports cleanly."""

from src import config


def test_truth():
    assert True


def test_config_imports():
    # Lazy env loading means importing config must never raise, even with no .env present.
    assert hasattr(config, "SOURCES_ORDER")
    assert hasattr(config, "WEIGHTS")
