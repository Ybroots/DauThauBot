from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Bid:
    tbmt_code: str
    title: str
    status: str
    investor: str
    posted_at: datetime
    field: str
    location: str
    closing_at: datetime
    bid_method: str
    detail_url: str
    budget_vnd: Optional[int] = None
    description: str = ""
    raw: Optional[dict[str, Any]] = field(default=None, repr=False)
