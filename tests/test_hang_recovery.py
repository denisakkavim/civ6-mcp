"""Tests for HANG recovery and post-load civ verification.

Covers the autosave cleanup logic and the GameState hang guard field.
"""

import os
from unittest.mock import MagicMock

import pytest

from civ_mcp.game_lifecycle import cleanup_old_autosaves
from civ_mcp.game_state import GameState


@pytest.fixture
def save_dir(tmp_path, monkeypatch):
    """Point autosave cleanup at a temporary directory."""
    monkeypatch.setattr("civ_mcp.game_launcher.SINGLE_SAVE_DIR", str(tmp_path))
    return tmp_path


def test_cleanup_keeps_the_n_most_recent(save_dir):
    """With 12 saves and keep=8, the 4 oldest are deleted."""
    for i in range(12):
        p = save_dir / f"0_MCP_{i:04d}.Civ6Save"
        p.write_text("x")
        os.utime(p, (1000 + i, 1000 + i))  # stagger mtimes

    cleanup_old_autosaves(keep=8)

    remaining = sorted(p.name for p in save_dir.glob("0_MCP_*.Civ6Save"))
    assert remaining == sorted(f"0_MCP_{i:04d}.Civ6Save" for i in range(4, 12))


def test_cleanup_deletes_nothing_under_the_limit(save_dir):
    for i in range(5):
        (save_dir / f"0_MCP_{i:04d}.Civ6Save").write_text("x")

    cleanup_old_autosaves(keep=8)
    assert len(list(save_dir.glob("0_MCP_*.Civ6Save"))) == 5


def test_cleanup_deletes_nothing_exactly_at_the_limit(save_dir):
    for i in range(8):
        (save_dir / f"0_MCP_{i:04d}.Civ6Save").write_text("x")

    cleanup_old_autosaves(keep=8)
    assert len(list(save_dir.glob("0_MCP_*.Civ6Save"))) == 8


def test_hang_retry_guard_starts_disarmed():
    assert GameState(MagicMock())._hang_retry_active is False
