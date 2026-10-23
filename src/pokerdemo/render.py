"""Replay real terminal bytes with official agg; extract unchanged replay frames."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from pokerdemo.tools import Tool


def render(capture: Path, agg: Tool, font_family: str) -> list[Path]:
    """Keep all evidence local and report rendering separately from game completion."""
    from PIL import Image, ImageSequence

    receipt_path = capture / "recording.json"
    receipt = json.loads(receipt_path.read_text())
    if not receipt.get("completed") or receipt.get("privateViews") is not False:
        raise ValueError("Expected a completed public-only recording")
    receipt["renderCompleted"] = False
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    before = agg.describe()
    subprocess.run(
        [
            agg.executable,
            "--font-family",
            font_family,
            "--font-size",
            "16",
            "--theme",
            "nord",
            "--idle-time-limit",
            "100000",
            str(capture / "session.cast"),
            str(capture / "demo.gif"),
        ],
        check=True,
    )
    if agg.describe() != before:
        raise RuntimeError("Renderer changed during capture conversion")
    remaining = {
        "connected": receipt["markers"]["connected"],
        "action": receipt["markers"]["flop"],
        "late-street": receipt["markers"]["river"],
        "result": receipt["markers"]["result"],
    }
    folder = capture / "frames"
    folder.mkdir(mode=0o700, exist_ok=False)
    names = {name: f"{index:02d}-{name}.png" for index, name in enumerate(remaining, 1)}
    frames: list[Path] = []
    elapsed = 0.0
    with Image.open(capture / "demo.gif") as image:
        for frame in ImageSequence.Iterator(image):
            duration = frame.info.get("duration", 100) / 1000
            for name, timestamp in tuple(remaining.items()):
                if elapsed + duration >= timestamp:
                    path = folder / names[name]
                    frame.convert("RGB").save(path)
                    frames.append(path)
                    del remaining[name]
            elapsed += duration
    if remaining:
        raise ValueError("Recording markers extend beyond the replay")
    receipt["tools"]["agg"] = before
    receipt["tools"]["Pillow"] = {"version": version("Pillow")}
    receipt["render"] = {
        "fontFamily": font_family,
        "fontSize": 16,
        "theme": "nord",
        "frames": "Unchanged agg replay frames; not native desktop screenshots",
    }
    receipt["filesSha256"] = {
        str(path.relative_to(capture)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(capture.rglob("*"))
        if path.is_file() and path != receipt_path
    }
    receipt["renderCompleted"] = True
    receipt["pipelineCompleted"] = True
    receipt["finishedAtUtc"] = datetime.now(UTC).isoformat()
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    return frames
