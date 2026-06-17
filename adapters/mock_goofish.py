from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Any

from .base import GoofishAdapter, RiskDetectedError


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


class MockGoofishAdapter(GoofishAdapter):
    name = "mock"

    def search_items(self, keyword: str, limit: int, account: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        keyword = (keyword or "相机").strip()
        regions = ["上海", "杭州", "深圳", "广州", "北京", "成都"]
        count = max(1, min(int(limit or 6), 20))
        batch_ts = int(time.time())
        items: list[dict[str, Any]] = []
        for index in range(count):
            price = round(random.uniform(80, 2800), 2)
            stable_id = index + 1
            item_id = f"mock-{keyword}-{stable_id}"
            sold_count = max(0, int((batch_ts // 120 + stable_id * 7) % 180) + random.randint(-2, 3))
            want_count = max(0, sold_count * random.randint(2, 5) + random.randint(0, 60))
            browse_count = max(20, want_count * random.randint(20, 65) + random.randint(0, 800))
            raw = {
                "keyword": keyword,
                "source": "mock_collector",
                "rank": index + 1,
                "sold_count": sold_count,
                "sales_volume": sold_count,
                "observed_at": _now_iso(),
            }
            items.append(
                {
                    "item_id": item_id,
                    "title": f"{keyword} 闲置样例 {index + 1}",
                    "price": price,
                    "original_price": round(price * random.uniform(1.05, 1.45), 2),
                    "region": random.choice(regions),
                    "seller_id": f"seller-{random.randint(100, 999)}",
                    "seller_nickname": f"卖家{random.randint(10, 99)}",
                    "want_count": want_count,
                    "browse_count": browse_count,
                    "sold_count": sold_count,
                    "sales_volume": sold_count,
                    "rank": index + 1,
                    "source": "mock",
                    "detail_url": f"https://local.mock/goofish/{item_id}",
                    "raw_json": raw,
                    "observed_at": _now_iso(),
                }
            )
        return items

    def publish_item(self, account: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
        text = f"{draft.get('title', '')} {draft.get('description', '')}"
        if any(token in text for token in ["风控", "验证码", "滑块", "risk"]):
            raise RiskDetectedError("模拟风控：内容触发人工复核")
        item_id = f"mock-{int(time.time())}-{random.randint(1000, 9999)}"
        return {"item_id": item_id, "detail_url": f"https://local.mock/items/{item_id}"}

    def send_reply(self, account: dict[str, Any], chat_id: str, text: str) -> dict[str, Any]:
        if "验证码" in text or "滑块" in text:
            raise RiskDetectedError("模拟风控：回复内容触发人工复核")
        return {"message_id": f"msg-{int(time.time())}-{random.randint(100, 999)}"}

    def deliver(self, account: dict[str, Any], order_id: str, content: str, auto_confirm: bool) -> dict[str, Any]:
        if "风控" in content:
            raise RiskDetectedError("模拟风控：发货内容触发人工复核")
        return {
            "delivery_id": f"delivery-{order_id}",
            "confirm_status": "success" if auto_confirm else "skipped",
        }
