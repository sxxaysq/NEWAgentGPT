"""Regression fixtures must never start background calls to a deployed model."""

import pytest


@pytest.fixture(autouse=True)
def isolated_memory_default(monkeypatch):
    monkeypatch.setenv("MEMORY_ENABLED", "false")
    monkeypatch.setenv("RAG_ENABLED", "false")
