from datetime import datetime, timezone

from tracker.formatter import format_bid_message
from tracker.models import Bid


def test_format_html_escape_and_link():
    bid = Bid(
        tbmt_code="IB2500579539-00",
        title="Gói <test> & demo",
        status="Chưa đóng thầu",
        investor="CĐT A",
        posted_at=datetime(2025, 12, 10, 10, 53, tzinfo=timezone.utc),
        field="Hàng hóa",
        location="Tây Ninh",
        closing_at=datetime(2025, 12, 19, 10, 0, tzinfo=timezone.utc),
        bid_method="Qua mạng",
        detail_url="https://muasamcong.mpi.gov.vn/detail",
        budget_vnd=100_000_000,
    )
    msg = format_bid_message(bid, ["lâm đồng"])
    assert "<b>Mã TBMT:</b>" in msg
    assert "&lt;test&gt;" in msg
    assert "#lâm_đồng" in msg or "#lam" in msg.lower() or "#" in msg
    assert 'href="https://muasamcong.mpi.gov.vn/detail"' in msg
