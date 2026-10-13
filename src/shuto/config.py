"""Configuration models and loading. Holds no secrets (see secrets.py)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import platformdirs
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .metadata import clean_place_part

APP_NAME = "shuto"
CONFIG_FILENAME = "shuto.yaml"
CONFIG_ENV_VAR = "SHUTO_CONFIG"


class ConfigError(ValueError):
    """Raised when the config file cannot be read or is invalid."""


def _expand(value: object) -> object:
    if value is None:
        return None
    return Path(os.path.expandvars(str(value))).expanduser()


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Source(_Section):
    path: Path
    recursive: bool = True

    @field_validator("path", mode="before")
    @classmethod
    def _expand_path(cls, v: object) -> object:
        return _expand(v)


class ImportConfig(_Section):
    batch_by: Literal["iso_week", "month", "day", "folder"] = "iso_week"
    near_duplicate_distance: int = Field(6, ge=0, le=64)
    extensions: list[str] = [".jpg", ".jpeg", ".heic", ".png"]


class CullConfig(_Section):
    min_megapixels: float = 4.0
    min_sharpness: float = 80.0
    mean_luma_range: tuple[float, float] = (35.0, 220.0)
    max_clipped_shadows_pct: float = 8.0
    max_clipped_highlights_pct: float = 5.0


class OllamaConfig(_Section):
    url: str = "http://127.0.0.1:11434"
    keep_alive: str = "10m"


class BedrockConfig(_Section):
    region: str | None = None
    profile: str | None = None


class LLMConfig(_Section):
    provider: Literal["ollama", "anthropic", "gemini", "bedrock", "fake"] = "ollama"
    model: str = "auto"
    image_max_side: int | None = None
    concurrency: int | None = None
    max_tokens: int = 1500
    temperature: float = 0.2
    json_retries: int = 1
    timeout_s: int = 300
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    bedrock: BedrockConfig = Field(default_factory=BedrockConfig)


class KeywordStyle(_Section):
    min: int = 25  # below this only a note (the sweet spot is 25 to 45)
    max: int = 45  # Shutterstock allows 50; padding to 50 looks like spam
    always_include: list[str] = []
    romaji_place_names: bool = False
    seasonal_terms: bool = True


class DescriptionStyle(_Section):
    """Lengths for the one-sentence description (Shutterstock has no separate title)."""

    target_min: int = Field(70, ge=20, le=200)  # shorter is only a note
    target_max: int = Field(150, ge=40, le=200)  # longer is only a note
    max: int = Field(200, ge=60, le=200)  # longer is a problem and is sent back to the AI once

    @model_validator(mode="after")
    def _ordered(self) -> DescriptionStyle:
        if not self.target_min <= self.target_max <= self.max:
            raise ValueError("description lengths must satisfy target_min <= target_max <= max")
        return self


class DefaultPlace(_Section):
    """Used in editorial captions when a photo has no known city or country."""

    city: str = ""
    country: str = ""

    @field_validator("city", "country")
    @classmethod
    def _check_part(cls, v: str) -> str:
        return clean_place_part(v)


class StyleConfig(_Section):
    theme: str = ""
    tone: str = "documentary, warm, factual; no hype words"
    language: str = "en"
    region_focus: Literal["", "japan"] = ""  # "japan": romaji, Japan-first rules, house rules
    keywords: KeywordStyle = Field(default_factory=KeywordStyle)
    avoid: list[str] = ["beautiful", "amazing", "stunning"]
    extra_instructions: str = ""
    default_place: DefaultPlace = Field(default_factory=DefaultPlace)
    description: DescriptionStyle = Field(default_factory=DescriptionStyle)


class FilterRuleConfig(_Section):
    kind: Literal["include", "avoid"]
    text: str
    mode: Literal["set_aside", "flag"] = "set_aside"
    id: str | None = None

    @field_validator("text")
    @classmethod
    def _check_text(cls, v: str) -> str:
        v = " ".join(v.split())
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
            raise ValueError("a rule must be plain text on one line")
        if not 2 <= len(v) <= 80:
            raise ValueError("a rule must be 2 to 80 characters long")
        return v


class FiltersConfig(_Section):
    theme_check: bool = True
    theme_partly: Literal["pass", "flag"] = "pass"
    rules: list[FilterRuleConfig] = Field(default_factory=list, max_length=10)


class RestrictionsConfig(_Section):
    builtin: bool = True
    extra_files: list[Path] = []
    disabled_rules: list[str] = []


class ReviewConfig(_Section):
    auto_approve_clean: bool = False


class PrepareConfig(_Section):
    filename_pattern: str = "{date:%Y%m%d}-{slug}-{n:03d}"
    slug_words: int = 5
    jpeg_quality: int = Field(95, ge=1, le=100)


class ReleasesConfig(_Section):
    dir: Path | None = None

    @field_validator("dir", mode="before")
    @classmethod
    def _expand_dir(cls, v: object) -> object:
        return _expand(v)


class DeliverConfig(_Section):
    ftps_host: str = "ftps.shutterstock.com"
    upload_enabled: bool = False


class WebConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(8765, ge=1, le=65535)
    open_browser: bool = True


class Config(_Section):
    workspace: Path | None = None
    sources: list[Source] = []
    import_: ImportConfig = Field(default_factory=ImportConfig, alias="import")
    cull: CullConfig = Field(default_factory=CullConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    style: StyleConfig = Field(default_factory=StyleConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    restrictions: RestrictionsConfig = Field(default_factory=RestrictionsConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    prepare: PrepareConfig = Field(default_factory=PrepareConfig)
    releases: ReleasesConfig = Field(default_factory=ReleasesConfig)
    deliver: DeliverConfig = Field(default_factory=DeliverConfig)
    web: WebConfig = Field(default_factory=WebConfig)

    @field_validator("workspace", mode="before")
    @classmethod
    def _expand_workspace(cls, v: object) -> object:
        return _expand(v)

    @property
    def workspace_dir(self) -> Path:
        return self.workspace if self.workspace is not None else default_workspace()

    def workspace_path(self, *parts: str) -> Path:
        return self.workspace_dir.joinpath(*parts)


def default_config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME))


def default_workspace() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME))


def find_config_path(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """--config > $SHUTO_CONFIG > ./shuto.yaml > user config dir. None if nothing exists."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(CONFIG_ENV_VAR)
    if env:
        return Path(env).expanduser()
    for candidate in (Path.cwd() / CONFIG_FILENAME, default_config_dir() / CONFIG_FILENAME):
        if candidate.is_file():
            return candidate
    return None


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"{loc}: {err['msg']}")
    return "; ".join(lines)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load the config; defaults when no file is found. An explicit missing path is an error."""
    cfg_path = find_config_path(path)
    if cfg_path is None:
        return Config()
    if not cfg_path.is_file():
        raise ConfigError(f"config file not found: {cfg_path}")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {cfg_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{cfg_path}: top level must be a mapping")
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{cfg_path}: {_format_errors(exc)}") from exc
