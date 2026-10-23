"""Replay real terminal bytes with official agg; extract unchanged replay frames."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from pokerdemo.tools import Tool


def render(capture: Path, agg: Tool, font_family: str) -> list[Path]:
    """Keep all evidence local and report rendering separately from game completion."""
    from PIL import Image

    receipt_path = capture / "recording.json"
    receipt = json.loads(receipt_path.read_text())
    if not receipt.get("completed") or receipt.get("privateViews") is not False:
        raise ValueError("Expected a completed public-only recording")
    receipt["renderCompleted"] = False
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    before = agg.describe()
    command = [
        agg.executable,
        "--renderer",
        "resvg",
        "--font-family",
        font_family,
        "--font-size",
        "16",
        "--theme",
        "nord",
        "--idle-time-limit",
        "100000",
    ]
    # A terminal game needs readable completed redraws, not every intermediate byte batch.
    subprocess.run(
        [*command, "--fps-cap", "1", str(capture / "session.cast"), str(capture / "demo.gif")],
        check=True,
    )
    selected = {
        "connected": receipt["markers"]["connected"],
        "action": receipt["markers"]["flop"],
        "late-street": receipt["markers"]["river"],
        "result": receipt["markers"]["result"],
    }
    folder = capture / "frames"
    folder.mkdir(mode=0o700, exist_ok=False)
    frames: list[Path] = []
    # Select exact moments from the same original PTY; GIF duration rounding is not a clock.
    # The temporary single-frame replays also stay inside this private run directory.
    with tempfile.TemporaryDirectory(prefix=".frame-replay-", dir=capture) as temporary:
        for index, (name, timestamp) in enumerate(selected.items(), 1):
            replay = Path(temporary) / f"{name}.gif"
            subprocess.run(
                [*command, "--select", str(timestamp), str(capture / "session.cast"), str(replay)],
                check=True,
            )
            path = folder / f"{index:02d}-{name}.png"
            with Image.open(replay) as image:
                if image.n_frames != 1:
                    raise ValueError("Expected one exact replay frame")
                image.convert("RGB").save(path)
            frames.append(path)
    if agg.describe() != before:
        raise RuntimeError("Renderer changed during capture conversion")
    receipt["tools"]["agg"] = before
    receipt["tools"]["Pillow"] = {"version": version("Pillow")}
    receipt["render"] = {
        "fontFamily": font_family,
        "fontSize": 16,
        "theme": "nord",
        "renderer": "resvg",
        "gifFpsCap": 1,
        "frameTimestamps": selected,
        "frames": "Exact agg --select replay frames from session.cast; not desktop screenshots",
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
