from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .base import GoofishAdapter, NotConfiguredError, RiskDetectedError


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
REAL_STATE_PATH = DATA_DIR / "real_state.json"
SEARCH_RESULTS_API_FRAGMENT = "/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
DEFAULT_BROWSER_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)


class RealGoofishAdapter(GoofishAdapter):
    """
    Scaffold for real protocol integration.

    Keep real platform logic inside this class or smaller modules it owns.
    The rest of the app should continue to talk only to this adapter interface,
    so publish/reply/delivery still go through queues, limits, and audit logs.
    """

    name = "real"

    def _require_login_state(self, account: dict[str, Any]) -> str:
        login_state = str(account.get("login_state") or "")
        if not login_state:
            raise NotConfiguredError("Real adapter requires an account login_state/cookie.")
        return login_state

    def search_items(self, keyword: str, limit: int, account: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return asyncio.run(_search_items_with_playwright(keyword, limit, account or {}))

    def publish_item(self, account: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
        self._require_login_state(account)
        raise NotConfiguredError(
            "Real publish is not implemented yet. Wire upload/category/publish protocol here."
        )

    def send_reply(self, account: dict[str, Any], chat_id: str, text: str) -> dict[str, Any]:
        self._require_login_state(account)
        raise NotConfiguredError(
            "Real IM reply is not implemented yet. Wire websocket/http message sending here."
        )

    def deliver(self, account: dict[str, Any], order_id: str, content: str, auto_confirm: bool) -> dict[str, Any]:
        self._require_login_state(account)
        raise NotConfiguredError(
            "Real delivery is not implemented yet. Wire delivery/confirm protocol here."
        )


def _is_search_results_response(response: Any) -> bool:
    request = getattr(response, "request", None)
    return (
        SEARCH_RESULTS_API_FRAGMENT in getattr(response, "url", "")
        and getattr(request, "method", "") == "POST"
    )


def _is_risk_or_login_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(
        marker in lowered
        for marker in (
            "passport.goofish.com",
            "mini_login",
            "login",
            "baxia",
            "punish",
            "validate",
            "captcha",
        )
    )


async def _search_items_with_playwright(keyword: str, limit: int, account: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise NotConfiguredError(
            "Real collection requires Playwright. Install with: python -m pip install playwright && python -m playwright install chromium"
        ) from exc

    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("keyword is required")
    limit = max(1, min(int(limit or 20), 50))
    login_state = str(account.get("login_state") or "").strip()
    state_path = _resolve_state_path()
    search_url = f"https://www.goofish.com/search?{urlencode({'q': keyword})}"

    async with async_playwright() as p:
        launch_options: dict[str, Any] = {"headless": True}
        executable_path = _resolve_browser_executable()
        if executable_path:
            launch_options["executable_path"] = executable_path
        browser = await p.chromium.launch(**launch_options)
        try:
            context_options: dict[str, Any] = {
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
                "viewport": {"width": 1365, "height": 900},
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/146.0.0.0 Safari/537.36"
                ),
            }
            if login_state:
                if login_state.startswith("{"):
                    context_options["storage_state"] = json.loads(login_state)
                else:
                    context_options["storage_state"] = _cookie_string_to_storage_state(login_state)
            elif state_path.exists():
                context_options["storage_state"] = str(state_path)
            context = await browser.new_context(**context_options)
            page = await context.new_page()
            response_future = page.wait_for_event(
                "response",
                predicate=_is_search_results_response,
                timeout=30_000,
            )
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
            if _is_risk_or_login_url(page.url):
                raise RiskDetectedError("搜索页跳转到登录/验证/风控页面，请先在浏览器完成登录后导入登录态。")
            try:
                response = await response_future
            except PlaywrightTimeoutError as exc:
                if _is_risk_or_login_url(page.url):
                    raise RiskDetectedError("等待搜索结果时触发登录/验证/风控页面。") from exc
                raise NotConfiguredError("没有捕获到闲鱼搜索结果接口；可能未登录、页面结构变化或需要更新登录态。") from exc
            data = await response.json()
            _raise_for_api_risk(data)
            items = _parse_search_response(data, keyword)[:limit]
            if not items:
                raise NotConfiguredError("搜索接口返回为空；请确认关键词、登录态和页面访问状态。")
            return items
        finally:
            await browser.close()


def _resolve_state_path() -> Path:
    env_value = str(os.environ.get("GOOFISH_STATE_FILE") or "").strip()
    if env_value:
        return Path(env_value).expanduser().resolve()
    return REAL_STATE_PATH


def _resolve_browser_executable() -> str | None:
    env_value = str(os.environ.get("GOOFISH_BROWSER_PATH") or "").strip()
    if env_value and Path(env_value).exists():
        return env_value
    for candidate in DEFAULT_BROWSER_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def _cookie_string_to_storage_state(cookie_string: str) -> dict[str, Any]:
    cookies = []
    for part in cookie_string.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".goofish.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    if not cookies:
        raise NotConfiguredError("Cookie string is empty or invalid.")
    return {"cookies": cookies, "origins": []}


def _raise_for_api_risk(data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False)[:4000]
    if any(marker in text for marker in ("FAIL_SYS_USER_VALIDATE", "验证码", "滑块", "风控", "登录", "punish", "baxia")):
        raise RiskDetectedError("搜索接口返回登录/验证/风控信号，已停止真实采集。")


def _parse_search_response(data: dict[str, Any], keyword: str) -> list[dict[str, Any]]:
    result_list = (((data or {}).get("data") or {}).get("resultList") or [])
    items: list[dict[str, Any]] = []
    observed_at = _now_iso()
    for index, wrapper in enumerate(result_list, start=1):
        main = (
            (((wrapper or {}).get("data") or {}).get("item") or {})
            .get("main", {})
            .get("exContent", {})
        )
        click_args = (
            (((wrapper or {}).get("data") or {}).get("item") or {})
            .get("main", {})
            .get("clickParam", {})
            .get("args", {})
        )
        item_id = str(main.get("itemId") or click_args.get("item_id") or click_args.get("itemId") or "").strip()
        title = _strip_text(main.get("title") or "")
        if not item_id or not title:
            continue
        price = _parse_price(main.get("price"))
        original_price = _parse_price(main.get("oriPrice"))
        target_url = str(main.get("targetUrl") or "")
        if target_url.startswith("fleamarket://"):
            target_url = target_url.replace("fleamarket://", "https://www.goofish.com/", 1)
        publish_time = _format_publish_time(click_args.get("publishTime"))
        sold_count = _first_int(
            main,
            click_args,
            keys=(
                "soldCount",
                "sold_count",
                "sellCount",
                "saleCount",
                "salesCount",
                "tradeCount",
                "dealCount",
                "orderCount",
                "quantitySold",
                "soldNum",
                "saleNum",
                "成交",
                "已售",
            ),
        )
        sales_volume = _first_int(
            main,
            click_args,
            keys=(
                "salesVolume",
                "sales_volume",
                "soldCount",
                "saleCount",
                "salesCount",
                "tradeCount",
                "soldNum",
                "saleNum",
            ),
        )
        raw = {
            "adapter": "real",
            "keyword": keyword,
            "rank": index,
            "publish_time": publish_time,
            "image": main.get("picUrl") or "",
            "tags": _extract_tags(main, click_args),
            "sold_count": sold_count,
            "sales_volume": sales_volume,
        }
        items.append(
            {
                "item_id": item_id,
                "title": title,
                "price": price,
                "original_price": original_price,
                "region": str(main.get("area") or ""),
                "seller_id": str(click_args.get("seller_id") or click_args.get("sellerId") or ""),
                "seller_nickname": str(main.get("userNickName") or ""),
                "want_count": _to_int(click_args.get("wantNum")),
                "browse_count": _to_int(click_args.get("browseCount")),
                "sold_count": sold_count,
                "sales_volume": sales_volume,
                "rank": index,
                "source": "real",
                "detail_url": target_url,
                "raw_json": raw,
                "observed_at": observed_at,
            }
        )
    return items


def _strip_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _parse_price(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        value = "".join(str(part.get("text", "")) for part in value if isinstance(part, dict))
    text = str(value).replace("当前价", "").replace("¥", "").replace(",", "").strip()
    if not text or text in {"暂无", "价格异常"}:
        return 0.0
    multiplier = 10000 if "万" in text else 1
    text = text.replace("万", "")
    match = re.search(r"\d+(?:\.\d+)?", text)
    return round(float(match.group(0)) * multiplier, 2) if match else 0.0


def _to_int(value: Any) -> int:
    try:
        text = str(value).replace(",", "").strip()
        multiplier = 10000 if "万" in text else 1
        text = text.replace("万", "")
        match = re.search(r"\d+(?:\.\d+)?", text)
        return int(float(match.group(0)) * multiplier) if match else 0
    except Exception:
        return 0


def _first_int(*sources: dict[str, Any], keys: tuple[str, ...]) -> int:
    for source in sources:
        for key in keys:
            if key in source:
                parsed = _to_int(source.get(key))
                if parsed:
                    return parsed
    text = json.dumps(sources, ensure_ascii=False)
    for label in ("已售", "成交", "卖出", "销量"):
        match = re.search(rf"{label}\D*(\d+(?:\.\d+)?万?)", text)
        if match:
            return _to_int(match.group(1))
    return 0


def _format_publish_time(value: Any) -> str:
    text = str(value or "")
    if not text.isdigit():
        return ""
    try:
        return datetime.fromtimestamp(int(text) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _extract_tags(main: dict[str, Any], click_args: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    if click_args.get("tag") == "freeship":
        tags.append("包邮")
    r1_tags = (((main.get("fishTags") or {}).get("r1") or {}).get("tagList") or [])
    for tag_item in r1_tags:
        content = (((tag_item or {}).get("data") or {}).get("content") or "")
        if content:
            tags.append(str(content))
    return tags


def _now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()
