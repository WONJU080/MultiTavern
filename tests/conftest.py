"""Tests use isolated settings, never the user's passwords or live backend."""

import pytest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.config import LLMConfig, ServerConfig, settings


@pytest.fixture
def tmp_path():
    """Remove each test's temporary files on teardown, even after a failure."""
    with TemporaryDirectory(prefix="anyworld-test-") as directory:
        yield Path(directory)


@pytest.fixture(autouse=True)
def isolated_working_directory(tmp_path, monkeypatch):
    """Contain default transcript/debug paths and restore cwd before deleting them."""
    monkeypatch.chdir(tmp_path)
    try:
        yield
    finally:
        monkeypatch.undo()


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Replace the global settings with isolated test values."""
    monkeypatch.setattr(
        settings,
        "server",
        ServerConfig(admin_password=None, disconnect_grace_seconds=0.0),
    )
    monkeypatch.setattr(
        settings,
        "llm",
        LLMConfig(
            provider="openai",
            api_key="test-only",
            model_name="test-only",
            system_prompt="Keep coherent public outcomes; never expose private DM guidance.",
            tokenizer_encoding=None,
        ),
    )


@pytest.fixture(autouse=True)
def deterministic_turn_order(monkeypatch):
    """Keep per-round turn order deterministic unless a test overrides shuffle."""
    monkeypatch.setattr("random.shuffle", lambda sequence: None)
