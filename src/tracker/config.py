from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator
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


class KeywordsConfig(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    min_budget_vnd: Optional[int] = None


def load_keywords(path: Path | None = None) -> KeywordsConfig:
    resolved = path or (PROJECT_ROOT / "config" / "keywords.yaml")
    with open(resolved, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return KeywordsConfig.model_validate(data)
