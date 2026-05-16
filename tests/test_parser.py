import json
from pathlib import Path

from tracker.parser import parse_search_response

FIXTURE = Path(__file__).parent / "fixtures" / "search_sample.json"


def test_parse_search_response():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bids = parse_search_response(data)
    assert len(bids) == 1
    bid = bids[0]
    assert bid.tbmt_code == "IB2500579539-00"
    assert "camera" in bid.title.lower()
    assert bid.field == "Hàng hóa"
    assert bid.bid_method == "Qua mạng"
    assert bid.budget_vnd == 500_000_000
