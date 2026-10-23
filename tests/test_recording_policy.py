"""Reject ambiguous private recordings before invoking the renderer or writing media."""

import json
from pathlib import Path

import pytest

from pokerdemo.runtime.render import render


@pytest.mark.parametrize(
    "receipt",
    [
        {"schema": 1, "completed": True, "privateViews": True},
        {"schema": True, "completed": True, "privateViews": False},
        {"schema": 2, "completed": True, "privateViews": "false"},
        {"schema": 2, "completed": False, "privateViews": False},
        {
            "schema": 2,
            "completed": True,
            "privateViews": True,
            "views": {"server": "private", "observer": "public", "players": "own-player"},
            "syntheticPlayers": ["alice", "bob"],
        },
        {
            "schema": 2,
            "completed": True,
            "privateViews": True,
            "views": {"server": "public", "observer": "public", "players": "own-player"},
            "syntheticPlayers": ["unknown"],
        },
    ],
)
def test_unsupported_receipt_cannot_create_rendered_media(tmp_path: Path, receipt: dict) -> None:
    path = tmp_path / "recording.json"
    original = json.dumps(receipt)
    path.write_text(original)
    with pytest.raises(ValueError):
        render(tmp_path, None, "unused")
    assert path.read_text() == original
    assert list(tmp_path.iterdir()) == [path]
