from __future__ import annotations

from typing import Any

from adapters import GoofishAdapter


def collect_items(
    adapter: GoofishAdapter,
    keyword: str,
    limit: int,
    account: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    items = adapter.search_items(keyword, limit, account)
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        item_id = str(item.get("item_id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not item_id or not title:
            continue
        raw = item.get("raw_json") or {}
        if isinstance(raw, dict):
            raw.setdefault("rank", index)
            raw.setdefault("adapter", adapter.name)
        rank = item.get("rank") or (raw.get("rank") if isinstance(raw, dict) else index)
        source = item.get("source") or (raw.get("adapter") if isinstance(raw, dict) else adapter.name)
        normalized.append(
            {
                "item_id": item_id,
                "title": title,
                "price": float(item.get("price") or 0),
                "original_price": float(item.get("original_price") or 0),
                "region": str(item.get("region") or ""),
                "seller_id": str(item.get("seller_id") or ""),
                "seller_nickname": str(item.get("seller_nickname") or ""),
                "want_count": int(item.get("want_count") or 0),
                "browse_count": int(item.get("browse_count") or 0),
                "sold_count": int(item.get("sold_count") or 0),
                "sales_volume": int(item.get("sales_volume") or item.get("sold_count") or 0),
                "rank": int(rank or index),
                "source": str(source or adapter.name),
                "detail_url": str(item.get("detail_url") or ""),
                "raw_json": raw,
                "observed_at": str(item.get("observed_at") or ""),
            }
        )
    return normalized
