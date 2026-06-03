from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Thư mục gốc dự án — không phụ thuộc cwd khi chạy module
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(PROJECT_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str
    telegram_chat_ids: str
    telegram_admin_chat_id: str = ""
    poll_interval_minutes: int = 45
    poll_jitter_seconds: int = 600
    log_level: str = "INFO"
    quiet_hours_start: str = "01:00"
    quiet_hours_end: str = "06:00"
    block_cooldown_hours: int = 6
    block_cooldown_max_hours: int = 48
    use_playwright: bool = True
    # Playwright: headless=false thường giúp reCAPTCHA v3; channel=chrome dùng Chrome đã cài (playwright install không đủ)
    playwright_headless: bool = True
    playwright_channel: Optional[str] = None
    # Số gói/lần ≈ crawl_max_pages × crawl_page_size (mặc định 2×50=100)
    crawl_page_size: int = 50
    crawl_max_pages: int = 2
    # true + có keywords.yaml: mỗi từ khóa gọi smart/search riêng (ES), gộp + dedupe theo mã TBMT.
    # false: chỉ một luồng “TBMT mới” như cũ (lọc từ khóa trên máy).
    crawl_per_keyword: bool = True
    # Nghỉ ngẫu nhiên giữa hai từ khóa (giảm 429) — chỉ khi crawl_per_keyword.
    crawl_keyword_gap_min_seconds: int = 6
    crawl_keyword_gap_max_seconds: int = 14

    @field_validator("playwright_channel", mode="before")
    @classmethod
    def _empty_playwright_channel(cls, v: object) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return str(v).strip() or None

    @field_validator("crawl_page_size")
    @classmethod
    def _clamp_page_size(cls, v: int) -> int:
        return max(10, min(v, 50))

    @field_validator("crawl_max_pages")
    @classmethod
    def _clamp_max_pages(cls, v: int) -> int:
        return max(1, min(v, 10))

    @field_validator("crawl_keyword_gap_min_seconds")
    @classmethod
    def _clamp_kw_gap_min(cls, v: int) -> int:
        return max(0, min(int(v), 300))

    @field_validator("crawl_keyword_gap_max_seconds")
    @classmethod
    def _clamp_kw_gap_max(cls, v: int) -> int:
        return max(0, min(int(v), 600))

    @model_validator(mode="after")
    def _keyword_gap_max_ge_min(self) -> Secrets:
        if self.crawl_keyword_gap_max_seconds < self.crawl_keyword_gap_min_seconds:
            self.crawl_keyword_gap_max_seconds = self.crawl_keyword_gap_min_seconds
        return self

    @property
    def crawl_max_bids(self) -> int:
        return self.crawl_max_pages * self.crawl_page_size

    # Tra cứu nhanh từ bot (/tim) — số tin tối đa mỗi lần, cooldown chống spam API
    interactive_search_max_messages: int = 15
    interactive_search_cooldown_seconds: int = 45
    # true: lọc /tim chặt — từ đơn phải khớp cả từ (không khớp nhầm cam⊂camera). Cụm nhiều từ vẫn khớp theo cụm liên tục.
    interactive_search_strict_keywords: bool = True
    # Số trang ES cho mỗi cụm /tim (mặc định = CRAWL_MAX_PAGES). Đặt nhỏ hơn (vd. 3) để /tim nhanh hơn khi cron cần nhiều trang.
    interactive_crawl_max_pages: Optional[int] = None

    @field_validator("interactive_crawl_max_pages")
    @classmethod
    def _clamp_interactive_pages(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return None
        return max(1, min(int(v), 10))

    # true: trong nhóm, tin thường (không lệnh) cũng hiểu là từ khóa tra ngay — dễ ồn nếu nhiều người chat
    bot_group_freeword: bool = False

    # true: trong nhóm, gửi một dòng gợi ý /tim khi không khớp lệnh
    bot_group_reply_hint: bool = False

    # ── Tender catalog search ─────────────────────────────────────────────────
    # true: /tim tìm DB trước (catalog tenders), Playwright là fallback khi không có kết quả.
    # false: luôn crawl trực tiếp (hành vi cũ).
    db_search_enabled: bool = True
    # Số kết quả tối đa trả về từ DB search (không ảnh hưởng live crawl cap).
    tender_search_limit: int = 10

    @property
    def interactive_fetch_max_pages(self) -> int:
        """Số trang smart/search cho mỗi cụm từ trong /tim."""
        if self.interactive_crawl_max_pages is None:
            return self.crawl_max_pages
        return max(1, min(self.interactive_crawl_max_pages, 10))

    @property
    def chat_id_list(self) -> list[str]:
        return [c.strip() for c in self.telegram_chat_ids.split(",") if c.strip()]

    @property
    def admin_chat_id(self) -> Optional[str]:
        value = self.telegram_admin_chat_id.strip()
        return value if value else None


class KeywordGroup(BaseModel):
    name: str
    require: Literal["all", "any"] = "all"
    keywords: list[str] = Field(default_factory=list)


class KeywordsConfig(BaseModel):
    groups: list[KeywordGroup] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "KeywordsConfig":
        if "groups" in data:
            return cls.model_validate(data)
        # Legacy flat keywords list → wrap as single OR group
        kws = [str(k) for k in (data.get("keywords") or []) if k]
        if kws:
            return cls(groups=[KeywordGroup(name="Default", require="any", keywords=kws)])
        return cls()


def _default_keywords_yaml_path() -> Path:
    """Ưu tiên KEYWORDS_YAML_PATH → /data/keywords.yaml → config/keywords.yaml → example."""
    env_p = os.environ.get("KEYWORDS_YAML_PATH", "").strip()
    if env_p:
        return Path(env_p)
    data_dir = os.environ.get("DATA_DIR", "").strip()
    if data_dir:
        vol = Path(data_dir) / "keywords.yaml"
        if vol.is_file():
            return vol
    local = PROJECT_ROOT / "config" / "keywords.yaml"
    if local.is_file():
        return local
    return PROJECT_ROOT / "config" / "keywords.example.yaml"


def load_keywords_yaml(path: Path | None = None) -> KeywordsConfig:
    """Load keywords config from YAML — only used for initial DB seed."""
    resolved = path or _default_keywords_yaml_path()
    with open(resolved, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return KeywordsConfig.from_dict(data)


def load_keywords(path: Path | None = None) -> KeywordsConfig:
    return load_keywords_yaml(path)
