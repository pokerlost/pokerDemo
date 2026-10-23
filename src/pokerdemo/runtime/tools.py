"""Discover prerequisites without installing tools or modifying the environment."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pokerdemo.settings import ENGINE_SHA256, ENGINE_WHEEL, Settings


@dataclass(frozen=True)
class Tool:
    name: str
    executable: str
    origin: str
    verified_binary: Path | None = None

    def describe(self) -> dict[str, str]:
        version = subprocess.check_output(
            [self.executable, "-V" if self.name == "tmux" else "--version"],
            text=True,
            timeout=10,
        ).strip()
        binary = self.verified_binary or Path(self.executable).resolve()
        return {
            "name": self.name,
            "version": version,
            "origin": self.origin,
            "executableSha256": hashlib.sha256(Path(self.executable).read_bytes()).hexdigest(),
            "binarySha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }


def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "codex_dev_team"


def resolve_tool(name: str, override: str | None = None) -> Tool:
    """Explicit executable > PATH > one checksum-verified existing local fallback."""
    executable = shutil.which(override or name)
    if executable:
        return Tool(name, executable, "explicit" if override else "PATH")
    if override:
        raise ValueError(f"Explicit {name} executable is unavailable")
    if name == "tmux":
        folder = cache_root() / "tools/tmux-3.6a"
        candidate = folder / "tmux"
        binary = folder / "root/usr/bin/tmux"
        expected = "79c1ab4e006c1e6829659443978d2db7303f5e3ab441c59b6abaf082557c658a"
        wrapper_hash = "4117ebdc14ac409501b3ebcc45fdb0bed81c24efa6cf3342630157c95a084460"
        if (
            not candidate.is_file()
            or hashlib.sha256(candidate.read_bytes()).hexdigest() != wrapper_hash
        ):
            raise ValueError(
                "tmux is missing: install your distribution package or set POKERDEMO_TMUX"
            )
    elif name == "agg":
        candidate = cache_root() / "tools/agg-1.9.0/agg"
        binary = candidate
        expected = "f111e315cd71056b116302342553dd765b7297579ed511f111d0cedb442aeda6"
    else:
        raise ValueError("Unsupported prerequisite")
    if (
        not binary.is_file()
        or not os.access(candidate, os.X_OK)
        or hashlib.sha256(binary.read_bytes()).hexdigest() != expected
    ):
        raise ValueError(
            f"{name} is missing: install the official utility or set POKERDEMO_{name.upper()}"
        )
    return Tool(name, str(candidate), "verified existing local cache; no installation", binary)


def resolve_wheel(settings: Settings) -> Path:
    if settings.engine_wheel is not None:
        candidate = settings.engine_wheel.expanduser().absolute()
    else:
        candidate = settings.workspace / ".local/wheels" / ENGINE_WHEEL

    if (
        not candidate.is_file()
        or hashlib.sha256(candidate.read_bytes()).hexdigest() != ENGINE_SHA256
    ):
        raise ValueError(
            "The exact pinned engine wheel is missing or changed; set POKERDEMO_ENGINE_WHEEL"
        )
    return candidate
