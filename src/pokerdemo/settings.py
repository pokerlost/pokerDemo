"""Lazy validated local configuration, inherited from the Typer foundation."""

from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENGINE_VERSION = "2.0.0.dev2"
ENGINE_WHEEL = "pokerengine-2.0.0.dev2-cp314-cp314-linux_x86_64.whl"
ENGINE_SHA256 = "13a1a57f6c61860cf1c1b89d1a4db624eda8d8cad132bdf7d61f0e94ab798ee3"


class Settings(BaseSettings):
    """Explicit options > environment > selected dotenv > defaults."""

    model_config = SettingsConfigDict(
        env_prefix="POKERDEMO_", env_file=None, extra="forbid", hide_input_in_errors=True
    )
    workspace: Path = Field(default_factory=Path.cwd)
    output_root: Path = Path(".local/recordings")
    engine_wheel: Path | None = None
    tmux: str | None = None
    agg: str | None = None
    delay: float = Field(default=1.5, ge=0.4, le=10, allow_inf_nan=False)
    font_family: str = "JetBrainsMono Nerd Font Mono"
    record_show_cards: bool = True


def load_settings(config: Path | None = None, **overrides: Any) -> Settings:
    """Read only an explicitly chosen dotenv; never load settings on import."""
    if config is not None and not config.is_file():
        raise OSError("Selected configuration file is unavailable")
    return Settings(_env_file=config, **{k: v for k, v in overrides.items() if v is not None})
