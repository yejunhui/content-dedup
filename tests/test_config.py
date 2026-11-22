from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dedup.config import AppConfig, ConfigError, load_config


def test_defaults_are_valid() -> None:
    config = AppConfig()
    config.validate()
    assert config.image.algorithm == "phash"
    assert config.safety.keep == "oldest"
    assert config.semantic.enabled is False


def test_unknown_section_and_key_rejected() -> None:
    with pytest.raises(ConfigError):
        AppConfig.from_dict({"unknown": {}})
    with pytest.raises(ConfigError):
        AppConfig.from_dict({"image": {"unknown": 1}})


def test_semantic_enabled_is_parseable_but_not_implemented() -> None:
    config = AppConfig.from_dict({"semantic": {"enabled": True}})
    assert config.semantic.enabled is True


def test_invalid_values_rejected() -> None:
    with pytest.raises(ConfigError):
        AppConfig.from_dict({"safety": {"keep": "random"}})
    with pytest.raises(ConfigError):
        AppConfig.from_dict({"image": {"hash_size": 2}})
    with pytest.raises(ConfigError):
        AppConfig.from_dict({"scan": {"workers": 0}})


def test_project_config_loads() -> None:
    path = Path(__file__).resolve().parents[1] / "config.yaml"
    config = load_config(path)
    assert config.image.color_threshold == 0.85
    assert config.video.frame_interval == 5.0
    assert config.audio.fpcalc == "fpcalc"


def test_load_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "custom.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scan": {"workers": 2, "exclude": [".git"]},
                "image": {"phash_threshold": 12},
                "safety": {"keep": "newest"},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.scan.workers == 2
    assert config.image.phash_threshold == 12
    assert config.safety.keep == "newest"
