from __future__ import annotations

from pathlib import Path

import pytest

from tracker.config import KeywordsConfig, _default_keywords_yaml_path, load_keywords


def test_load_keywords_explicit_path(tmp_path: Path) -> None:
    p = tmp_path / "k.yaml"
    p.write_text(
        "keywords: [x]\nlocations: []\nfields: []\nmin_budget_vnd:\n",
        encoding="utf-8",
    )
    cfg = load_keywords(path=p)
    assert isinstance(cfg, KeywordsConfig)
    # Legacy flat keywords are wrapped into a single OR group
    assert len(cfg.groups) == 1
    assert cfg.groups[0].require == "any"
    assert "x" in cfg.groups[0].keywords


def test_default_keywords_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    p = tmp_path / "only.yaml"
    p.write_text(
        "keywords: []\nlocations: []\nfields: []\nmin_budget_vnd:\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYWORDS_YAML_PATH", str(p))
    monkeypatch.delenv("DATA_DIR", raising=False)
    assert _default_keywords_yaml_path() == p
