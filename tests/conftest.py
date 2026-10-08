"""Keep continuity tests independent of the caller's live handoff ledger."""
import pytest


@pytest.fixture(autouse=True)
def isolate_handoff_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HANDOFF_LEDGER_DIR", str(tmp_path / "handoff-ledger"))


@pytest.fixture(autouse=True)
def isolate_continuity_config(tmp_path, monkeypatch):
    """The live ~/.config/continuity/config.json names where handoffs are stored (#1572)."""
    monkeypatch.setenv("CONTINUITY_CONFIG", str(tmp_path / "continuity-config.json"))


@pytest.fixture(autouse=True)
def isolate_codex_identity(monkeypatch):
    """No test takes its invoking worker's Codex session as an input."""
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CONTINUITY_HARNESS", raising=False)
