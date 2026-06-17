#!/usr/bin/env python
"""
Local Xianyu operations workbench MVP.

This is intentionally dependency-free: Python stdlib + SQLite + static files.
High-risk actions such as publish, auto-reply, and delivery are modeled as
queued, audited jobs. The current adapter is a local simulator; replace
MockGoofishAdapter with a real adapter when integrating platform protocols.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import ctypes.wintypes
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from adapters import RiskDetectedError, build_adapter
from services.collector import collect_items


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"
DB_PATH = DATA_DIR / "workbench.sqlite3"
SECRET_KEY_PATH = DATA_DIR / "local_secret.key"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
ADAPTER_MODE = os.environ.get("GOOFISH_ADAPTER", "mock")

DB_LOCK = threading.RLock()
STOP_EVENT = threading.Event()
LOGIN_SESSIONS: dict[str, "LoginCaptureSession"] = {}
LOGIN_SESSIONS_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(row) for row in rows if row is not None]


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    with DB_LOCK:
        with get_db() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return int(cur.lastrowid or 0)


def query(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with DB_LOCK:
        with get_db() as conn:
            return list(conn.execute(sql, params).fetchall())


def query_one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    schema = [
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            platform_user_id TEXT,
            encrypted_login_state TEXT,
            login_state_hint TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            auto_publish_enabled INTEGER NOT NULL DEFAULT 0,
            auto_reply_enabled INTEGER NOT NULL DEFAULT 1,
            auto_delivery_enabled INTEGER NOT NULL DEFAULT 0,
            daily_publish_limit INTEGER NOT NULL DEFAULT 5,
            published_today INTEGER NOT NULL DEFAULT 0,
            last_health_check TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            price REAL NOT NULL,
            original_price REAL,
            region TEXT,
            seller_id TEXT,
            seller_nickname TEXT,
            want_count INTEGER DEFAULT 0,
            browse_count INTEGER DEFAULT 0,
            sold_count INTEGER DEFAULT 0,
            sales_volume INTEGER DEFAULT 0,
            last_rank INTEGER,
            last_keyword TEXT,
            source TEXT,
            detail_url TEXT,
            raw_json TEXT,
            observed_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS price_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            price REAL NOT NULL,
            original_price REAL,
            observed_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            item_id TEXT NOT NULL,
            title TEXT NOT NULL,
            price REAL NOT NULL,
            original_price REAL,
            region TEXT,
            seller_id TEXT,
            seller_nickname TEXT,
            want_count INTEGER DEFAULT 0,
            browse_count INTEGER DEFAULT 0,
            sold_count INTEGER DEFAULT 0,
            sales_volume INTEGER DEFAULT 0,
            rank INTEGER,
            source TEXT,
            observed_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS publish_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            source_item_id TEXT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            price REAL NOT NULL,
            images_json TEXT DEFAULT '[]',
            address TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS publish_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            scheduled_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            require_manual_confirm INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            result_item_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(draft_id) REFERENCES publish_drafts(id) ON DELETE CASCADE,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            sender_name TEXT,
            item_id TEXT,
            inbound_text TEXT NOT NULL,
            reply_strategy TEXT,
            generated_reply TEXT,
            sent_reply TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reply_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            keyword TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 10,
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS delivery_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'text',
            content TEXT NOT NULL,
            auto_confirm INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            cooldown_seconds INTEGER NOT NULL DEFAULT 600,
            last_sent_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS delivery_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            rule_id INTEGER,
            order_id TEXT NOT NULL,
            buyer_name TEXT,
            item_title TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            confirm_status TEXT NOT NULL DEFAULT 'skipped',
            content_sent TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            FOREIGN KEY(rule_id) REFERENCES delivery_rules(id) ON DELETE SET NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS risk_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            level TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            action_taken TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            account_id INTEGER,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT,
            details_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ]
    with DB_LOCK:
        with get_db() as conn:
            for statement in schema:
                conn.execute(statement)
            ensure_column(conn, "items", "sold_count", "INTEGER DEFAULT 0")
            ensure_column(conn, "items", "sales_volume", "INTEGER DEFAULT 0")
            ensure_column(conn, "items", "last_rank", "INTEGER")
            ensure_column(conn, "items", "last_keyword", "TEXT")
            ensure_column(conn, "items", "source", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_market_snapshots_keyword_time ON market_snapshots(keyword, observed_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_market_snapshots_item_time ON market_snapshots(item_id, observed_at)")
            conn.execute(
                """
                INSERT INTO market_snapshots(
                    keyword, item_id, title, price, original_price, region, seller_id, seller_nickname,
                    want_count, browse_count, sold_count, sales_volume, rank, source, observed_at
                )
                SELECT
                    COALESCE(items.last_keyword, ''),
                    price_snapshots.item_id,
                    COALESCE(items.title, price_snapshots.item_id),
                    price_snapshots.price,
                    price_snapshots.original_price,
                    COALESCE(items.region, ''),
                    COALESCE(items.seller_id, ''),
                    COALESCE(items.seller_nickname, ''),
                    COALESCE(items.want_count, 0),
                    COALESCE(items.browse_count, 0),
                    COALESCE(items.sold_count, 0),
                    COALESCE(items.sales_volume, 0),
                    items.last_rank,
                    COALESCE(items.source, 'legacy_price_snapshot'),
                    price_snapshots.observed_at
                FROM price_snapshots
                LEFT JOIN items ON items.item_id = price_snapshots.item_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM market_snapshots
                    WHERE market_snapshots.item_id = price_snapshots.item_id
                      AND market_snapshots.observed_at = price_snapshots.observed_at
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO settings(key, value, updated_at)
                VALUES('global_kill_switch', 'false', ?)
                """,
                (now_iso(),),
            )
            conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def audit(
    action: str,
    target_type: str,
    target_id: Any = None,
    account_id: int | None = None,
    details: dict[str, Any] | None = None,
    actor: str = "local-admin",
) -> None:
    execute(
        """
        INSERT INTO audit_logs(actor, account_id, action, target_type, target_id, details_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor,
            account_id,
            action,
            target_type,
            str(target_id) if target_id is not None else None,
            json.dumps(details or {}, ensure_ascii=False),
            now_iso(),
        ),
    )


def add_risk_event(account_id: int | None, level: str, event_type: str, message: str, action_taken: str) -> None:
    execute(
        """
        INSERT INTO risk_events(account_id, level, event_type, message, action_taken, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (account_id, level, event_type, message, action_taken, now_iso()),
    )
    if account_id:
        execute("UPDATE accounts SET status = 'risk_paused' WHERE id = ?", (account_id,))
    audit(
        "risk_event",
        "account" if account_id else "system",
        account_id,
        account_id,
        {"level": level, "event_type": event_type, "message": message, "action_taken": action_taken},
    )


def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return str(row["value"]) if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        """
        INSERT INTO settings(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now_iso()),
    )
    audit("update_setting", "setting", key, None, {"value": value})


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _local_key() -> bytes:
    if not SECRET_KEY_PATH.exists():
        SECRET_KEY_PATH.write_bytes(os.urandom(32))
    return SECRET_KEY_PATH.read_bytes()


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    return bytes(byte ^ key[index % len(key)] for index, byte in enumerate(data))


def protect_secret(value: str) -> str:
    raw = value.encode("utf-8")
    if os.name == "nt":
        try:
            return "dpapi:" + base64.b64encode(_dpapi_protect(raw)).decode("ascii")
        except Exception:
            pass
    protected = _xor_bytes(raw, _local_key())
    return "devxor:" + base64.b64encode(protected).decode("ascii")


def unprotect_secret(value: str) -> str:
    if value.startswith("dpapi:") and os.name == "nt":
        raw = base64.b64decode(value.split(":", 1)[1])
        return _dpapi_unprotect(raw).decode("utf-8")
    if value.startswith("devxor:"):
        raw = base64.b64decode(value.split(":", 1)[1])
        return _xor_bytes(raw, _local_key()).decode("utf-8")
    return ""


def login_state_hint(login_state: str) -> str:
    if not login_state:
        return ""
    return f"已保存 {len(login_state)} 字符，尾号 {login_state[-4:]}"


ADAPTER = build_adapter(ADAPTER_MODE)


def public_account(row: sqlite3.Row) -> dict[str, Any]:
    data = row_to_dict(row) or {}
    data.pop("encrypted_login_state", None)
    data["has_login_state"] = bool(row["encrypted_login_state"])
    return data


def adapter_account(row: sqlite3.Row) -> dict[str, Any]:
    data = public_account(row)
    encrypted = str(row["encrypted_login_state"] or "")
    data["login_state"] = unprotect_secret(encrypted) if encrypted else ""
    return data


class LoginCaptureSession:
    def __init__(self, account_id: int):
        self.id = uuid.uuid4().hex[:12]
        self.account_id = account_id
        self.created_at = now_iso()
        self.user_data_dir = DATA_DIR / "login-capture" / self.id
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name=f"login-capture-{self.id}", daemon=True)
        self.thread.start()
        self.playwright = None
        self.context = None
        self.page = None
        self.error = ""
        self.closed = False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _run(self, coro: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=90)

    def start(self) -> dict[str, Any]:
        self._run(self._start_async())
        return self.status()

    async def _start_async(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            raise RuntimeError("Playwright 未安装，无法打开登录窗口。请先运行 python -m pip install playwright") from exc
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        browser_path = resolve_browser_executable()
        launch_options: dict[str, Any] = {
            "headless": False,
            "viewport": {"width": 1280, "height": 860},
        }
        if browser_path:
            launch_options["executable_path"] = browser_path
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(self.user_data_dir),
            **launch_options,
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self.page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=60000)

    def status(self) -> dict[str, Any]:
        if self.closed:
            return {"id": self.id, "account_id": self.account_id, "status": "closed", "created_at": self.created_at}
        try:
            return self._run(self._status_async())
        except Exception as exc:
            self.error = str(exc)
            return {
                "id": self.id,
                "account_id": self.account_id,
                "status": "error",
                "error": self.error,
                "created_at": self.created_at,
            }

    async def _status_async(self) -> dict[str, Any]:
        if not self.context or not self.page:
            return {"id": self.id, "account_id": self.account_id, "status": "starting", "created_at": self.created_at}
        cookies = await self.context.cookies(["https://www.goofish.com", "https://goofish.com", "https://www.taobao.com"])
        cookie_names = sorted({cookie.get("name", "") for cookie in cookies if cookie.get("name")})
        page_url = self.page.url
        title = await self.page.title()
        login_cookie_markers = {"unb", "cookie2", "_m_h5_tk", "sgcookie", "cna", "xlly_s"}
        has_cookie_signal = any(name in cookie_names for name in login_cookie_markers)
        return {
            "id": self.id,
            "account_id": self.account_id,
            "status": "open",
            "created_at": self.created_at,
            "url": page_url,
            "title": title,
            "cookie_count": len(cookies),
            "cookie_signal": has_cookie_signal,
            "cookie_names_hint": cookie_names[:12],
        }

    def goto(self, url: str) -> dict[str, Any]:
        return self._run(self._goto_async(url))

    async def _goto_async(self, url: str) -> dict[str, Any]:
        if not self.page:
            raise RuntimeError("登录窗口尚未打开")
        if not url.startswith(("https://www.goofish.com/", "https://goofish.com/")):
            raise ValueError("登录向导只允许打开 goofish.com 页面")
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        return await self._status_async()

    def save_to_account(self) -> dict[str, Any]:
        return self._run(self._save_to_account_async())

    async def _save_to_account_async(self) -> dict[str, Any]:
        if not self.context:
            raise RuntimeError("登录窗口尚未打开")
        state = await self.context.storage_state()
        cookies = state.get("cookies") or []
        if not cookies:
            raise RuntimeError("没有捕获到 Cookie，请先在打开的闲鱼窗口里完成登录")
        login_state = json.dumps(state, ensure_ascii=False)
        execute(
            """
            UPDATE accounts
            SET encrypted_login_state = ?, login_state_hint = ?, last_health_check = ?, status = 'active'
            WHERE id = ?
            """,
            (protect_secret(login_state), login_state_hint(login_state), now_iso(), self.account_id),
        )
        audit(
            "capture_login_state",
            "account",
            self.account_id,
            self.account_id,
            {"session_id": self.id, "cookie_count": len(cookies)},
        )
        return public_account(query_one("SELECT * FROM accounts WHERE id = ?", (self.account_id,)))

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._run(self._close_async())
        finally:
            self.closed = True
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)

    async def _close_async(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None


def resolve_browser_executable() -> str | None:
    candidates = [
        os.environ.get("GOOFISH_BROWSER_PATH", ""),
        r"E:\tools\115Chrome\Application\115chrome.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "115Chrome" / "Application" / "115chrome.exe"),
        str(Path(os.environ.get("ProgramFiles", "")) / "115Chrome" / "Application" / "115chrome.exe"),
        str(Path(os.environ.get("ProgramFiles(x86)", "")) / "115Chrome" / "Application" / "115chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def start_login_capture(account_id: int) -> dict[str, Any]:
    account = query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not account:
        raise ValueError("账号不存在")
    session = LoginCaptureSession(account_id)
    with LOGIN_SESSIONS_LOCK:
        LOGIN_SESSIONS[session.id] = session
    try:
        return session.start()
    except Exception:
        with LOGIN_SESSIONS_LOCK:
            LOGIN_SESSIONS.pop(session.id, None)
        session.close()
        raise


def get_login_capture(session_id: str) -> dict[str, Any]:
    with LOGIN_SESSIONS_LOCK:
        session = LOGIN_SESSIONS.get(session_id)
    if not session:
        raise ValueError("登录窗口不存在或已关闭")
    return session.status()


def goto_login_capture(session_id: str, url: str) -> dict[str, Any]:
    with LOGIN_SESSIONS_LOCK:
        session = LOGIN_SESSIONS.get(session_id)
    if not session:
        raise ValueError("登录窗口不存在或已关闭")
    return session.goto(url)


def save_login_capture(session_id: str) -> dict[str, Any]:
    with LOGIN_SESSIONS_LOCK:
        session = LOGIN_SESSIONS.get(session_id)
    if not session:
        raise ValueError("登录窗口不存在或已关闭")
    account = session.save_to_account()
    session.close()
    with LOGIN_SESSIONS_LOCK:
        LOGIN_SESSIONS.pop(session_id, None)
    return {"ok": True, "account": account}


def close_login_capture(session_id: str) -> dict[str, Any]:
    with LOGIN_SESSIONS_LOCK:
        session = LOGIN_SESSIONS.pop(session_id, None)
    if session:
        session.close()
    return {"ok": True}


def create_account(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("账号名称不能为空")
    login_state = str(payload.get("login_state") or "")
    account_id = execute(
        """
        INSERT INTO accounts(
            name, platform_user_id, encrypted_login_state, login_state_hint, status,
            auto_publish_enabled, auto_reply_enabled, auto_delivery_enabled,
            daily_publish_limit, created_at
        )
        VALUES(?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
        """,
        (
            name,
            str(payload.get("platform_user_id") or "").strip(),
            protect_secret(login_state) if login_state else "",
            login_state_hint(login_state),
            1 if payload.get("auto_publish_enabled") else 0,
            0 if payload.get("auto_reply_enabled") is False else 1,
            1 if payload.get("auto_delivery_enabled") else 0,
            int(payload.get("daily_publish_limit") or 5),
            now_iso(),
        ),
    )
    audit("create_account", "account", account_id, account_id, {"name": name})
    return public_account(query_one("SELECT * FROM accounts WHERE id = ?", (account_id,)))


def update_account(account_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "name",
        "platform_user_id",
        "status",
        "auto_publish_enabled",
        "auto_reply_enabled",
        "auto_delivery_enabled",
        "daily_publish_limit",
    }
    fields: list[str] = []
    params: list[Any] = []
    for key in allowed:
        if key not in payload:
            continue
        value = payload[key]
        if key.startswith("auto_"):
            value = 1 if value else 0
        if key == "daily_publish_limit":
            value = int(value)
        fields.append(f"{key} = ?")
        params.append(value)
    if "login_state" in payload:
        login_state = str(payload.get("login_state") or "")
        fields.append("encrypted_login_state = ?")
        fields.append("login_state_hint = ?")
        params.append(protect_secret(login_state) if login_state else "")
        params.append(login_state_hint(login_state))
    if not fields:
        raise ValueError("没有可更新字段")
    params.append(account_id)
    execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id = ?", tuple(params))
    audit("update_account", "account", account_id, account_id, {k: v for k, v in payload.items() if k != "login_state"})
    row = query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not row:
        raise ValueError("账号不存在")
    return public_account(row)


def seed_demo() -> dict[str, Any]:
    account = query_one("SELECT * FROM accounts LIMIT 1")
    if not account:
        account = sqlite3.Row
        account_data = create_account(
            {
                "name": "本地演示账号",
                "platform_user_id": "demo_unb",
                "login_state": "demo-cookie-will-not-be-used",
                "auto_publish_enabled": True,
                "auto_reply_enabled": True,
                "auto_delivery_enabled": True,
                "daily_publish_limit": 3,
            }
        )
        account_id = int(account_data["id"])
    else:
        account_id = int(account["id"])
    if not query_one("SELECT * FROM reply_rules LIMIT 1"):
        create_reply_rule({"account_id": account_id, "keyword": "在吗", "reply_text": "您好，在的，请问看中哪件？", "priority": 20})
        create_reply_rule({"account_id": account_id, "keyword": "包邮", "reply_text": "可以包邮，今天下单会尽快处理。", "priority": 15})
    if not query_one("SELECT * FROM delivery_rules LIMIT 1"):
        create_delivery_rule(
            {
                "account_id": account_id,
                "keyword": "虚拟资料",
                "content_type": "text",
                "content": "资料链接：请替换为你的真实交付内容。",
                "auto_confirm": True,
                "cooldown_seconds": 600,
            }
        )
    created = run_collection("相机", 6)
    audit("seed_demo", "system", None, account_id, {"items_created": created})
    return {"ok": True, "account_id": account_id, "items_created": created}


def run_collection(keyword: str, limit: int = 6, account_id: int | None = None) -> int:
    keyword = (keyword or "相机").strip()
    count = max(1, min(int(limit or 6), 20))
    account_for_adapter = None
    if account_id:
        account_row = query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        if not account_row:
            raise ValueError("采集账号不存在")
        account_for_adapter = adapter_account(account_row)
    items = collect_items(ADAPTER, keyword, count, account_for_adapter)
    created = 0
    for item in items:
        observed_at = item.get("observed_at") or now_iso()
        raw = item.get("raw_json") or {}
        source = str(item.get("source") or raw.get("adapter") or ADAPTER.name)
        rank = item.get("rank")
        rank_value = int(rank) if str(rank or "").isdigit() else None
        sold_count = int(item.get("sold_count") or 0)
        sales_volume = int(item.get("sales_volume") or sold_count or 0)
        execute(
            """
            INSERT OR REPLACE INTO items(
                item_id, title, price, original_price, region, seller_id, seller_nickname,
                want_count, browse_count, sold_count, sales_volume, last_rank, last_keyword,
                source, detail_url, raw_json, observed_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["item_id"],
                item["title"],
                float(item["price"]),
                float(item["original_price"] or 0),
                item["region"],
                item["seller_id"],
                item["seller_nickname"],
                int(item["want_count"] or 0),
                int(item["browse_count"] or 0),
                sold_count,
                sales_volume,
                rank_value,
                keyword,
                source,
                item["detail_url"],
                json.dumps(raw, ensure_ascii=False),
                observed_at,
            ),
        )
        execute(
            "INSERT INTO price_snapshots(item_id, price, original_price, observed_at) VALUES(?, ?, ?, ?)",
            (item["item_id"], float(item["price"]), float(item["original_price"] or 0), observed_at),
        )
        execute(
            """
            INSERT INTO market_snapshots(
                keyword, item_id, title, price, original_price, region, seller_id, seller_nickname,
                want_count, browse_count, sold_count, sales_volume, rank, source, observed_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                keyword,
                item["item_id"],
                item["title"],
                float(item["price"]),
                float(item["original_price"] or 0),
                item["region"],
                item["seller_id"],
                item["seller_nickname"],
                int(item["want_count"] or 0),
                int(item["browse_count"] or 0),
                sold_count,
                sales_volume,
                rank_value,
                source,
                observed_at,
            ),
        )
        created += 1
    audit("run_collection", "collector", keyword, account_id, {"count": created, "adapter": ADAPTER.name})
    return created


def create_publish_draft(payload: dict[str, Any]) -> dict[str, Any]:
    account_id = int(payload.get("account_id") or 0)
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    price = float(payload.get("price") or 0)
    if not account_id or not title or not description or price <= 0:
        raise ValueError("发布草稿需要账号、标题、描述和有效价格")
    draft_id = execute(
        """
        INSERT INTO publish_drafts(account_id, source_item_id, title, description, price, images_json, address, status, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, 'draft', ?)
        """,
        (
            account_id,
            str(payload.get("source_item_id") or ""),
            title,
            description,
            price,
            json.dumps(payload.get("images") or [], ensure_ascii=False),
            str(payload.get("address") or ""),
            now_iso(),
        ),
    )
    audit("create_publish_draft", "publish_draft", draft_id, account_id, {"title": title, "price": price})
    return row_to_dict(query_one("SELECT * FROM publish_drafts WHERE id = ?", (draft_id,)))


def create_publish_job(payload: dict[str, Any]) -> dict[str, Any]:
    draft_id = int(payload.get("draft_id") or 0)
    draft = query_one("SELECT * FROM publish_drafts WHERE id = ?", (draft_id,))
    if not draft:
        raise ValueError("发布草稿不存在")
    account_id = int(payload.get("account_id") or draft["account_id"])
    mode = str(payload.get("mode") or "confirm")
    if mode not in {"confirm", "auto"}:
        raise ValueError("发布模式必须是 confirm 或 auto")
    status = "paused" if mode == "confirm" else "queued"
    last_error = "等待人工确认" if mode == "confirm" else None
    job_id = execute(
        """
        INSERT INTO publish_jobs(
            draft_id, account_id, mode, status, scheduled_at, require_manual_confirm,
            last_error, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            draft_id,
            account_id,
            mode,
            status,
            str(payload.get("scheduled_at") or now_iso()),
            1 if mode == "confirm" else 0,
            last_error,
            now_iso(),
            now_iso(),
        ),
    )
    execute("UPDATE publish_drafts SET status = 'queued' WHERE id = ?", (draft_id,))
    audit("create_publish_job", "publish_job", job_id, account_id, {"draft_id": draft_id, "mode": mode, "status": status})
    return row_to_dict(query_one("SELECT * FROM publish_jobs WHERE id = ?", (job_id,)))


def confirm_publish_job(job_id: int) -> dict[str, Any]:
    row = query_one("SELECT * FROM publish_jobs WHERE id = ?", (job_id,))
    if not row:
        raise ValueError("发布任务不存在")
    execute(
        """
        UPDATE publish_jobs
        SET status = 'queued', last_error = NULL, require_manual_confirm = 0, updated_at = ?
        WHERE id = ?
        """,
        (now_iso(), job_id),
    )
    audit("confirm_publish_job", "publish_job", job_id, int(row["account_id"]), {"previous_status": row["status"]})
    return row_to_dict(query_one("SELECT * FROM publish_jobs WHERE id = ?", (job_id,)))


def pause_publish_job(job_id: int) -> dict[str, Any]:
    row = query_one("SELECT * FROM publish_jobs WHERE id = ?", (job_id,))
    if not row:
        raise ValueError("发布任务不存在")
    execute(
        "UPDATE publish_jobs SET status = 'paused', last_error = '人工暂停', updated_at = ? WHERE id = ?",
        (now_iso(), job_id),
    )
    audit("pause_publish_job", "publish_job", job_id, int(row["account_id"]), {})
    return row_to_dict(query_one("SELECT * FROM publish_jobs WHERE id = ?", (job_id,)))


def create_reply_rule(payload: dict[str, Any]) -> dict[str, Any]:
    keyword = str(payload.get("keyword") or "").strip()
    reply_text = str(payload.get("reply_text") or "").strip()
    if not keyword or not reply_text:
        raise ValueError("回复规则需要关键词和回复内容")
    rule_id = execute(
        """
        INSERT INTO reply_rules(account_id, keyword, reply_text, enabled, priority, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            int(payload["account_id"]) if payload.get("account_id") else None,
            keyword,
            reply_text,
            0 if payload.get("enabled") is False else 1,
            int(payload.get("priority") or 10),
            now_iso(),
        ),
    )
    audit("create_reply_rule", "reply_rule", rule_id, payload.get("account_id"), {"keyword": keyword})
    return row_to_dict(query_one("SELECT * FROM reply_rules WHERE id = ?", (rule_id,)))


def simulate_message(payload: dict[str, Any]) -> dict[str, Any]:
    account_id = int(payload.get("account_id") or 0)
    text = str(payload.get("inbound_text") or "").strip()
    if not account_id or not text:
        raise ValueError("模拟消息需要账号和消息内容")
    account = query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not account:
        raise ValueError("账号不存在")
    if any(token in text for token in ["滑块", "验证码", "风控"]):
        add_risk_event(account_id, "high", "im_risk", "消息触发风控关键词，账号已暂停", "pause_account")
        status = "paused"
        strategy = "risk_pause"
        generated = ""
        sent = ""
    elif account["status"] != "active" or not account["auto_reply_enabled"]:
        status = "paused"
        strategy = "manual_takeover"
        generated = ""
        sent = ""
    else:
        rules = query(
            """
            SELECT * FROM reply_rules
            WHERE enabled = 1 AND (account_id IS NULL OR account_id = ?)
            ORDER BY priority DESC, id ASC
            """,
            (account_id,),
        )
        matched = next((rule for rule in rules if str(rule["keyword"]) in text), None)
        if matched:
            strategy = f"keyword:{matched['keyword']}"
            generated = str(matched["reply_text"])
        else:
            strategy = "default"
            generated = "您好，消息已收到，我稍后确认后回复您。"
        ADAPTER.send_reply(adapter_account(account), str(payload.get("chat_id") or "chat-demo"), generated)
        status = "replied"
        sent = generated
    message_id = execute(
        """
        INSERT INTO messages(account_id, chat_id, sender_name, item_id, inbound_text, reply_strategy,
                             generated_reply, sent_reply, status, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            str(payload.get("chat_id") or "chat-demo"),
            str(payload.get("sender_name") or "买家"),
            str(payload.get("item_id") or ""),
            text,
            strategy,
            generated,
            sent,
            status,
            now_iso(),
        ),
    )
    audit("simulate_message", "message", message_id, account_id, {"status": status, "strategy": strategy})
    return row_to_dict(query_one("SELECT * FROM messages WHERE id = ?", (message_id,)))


def create_delivery_rule(payload: dict[str, Any]) -> dict[str, Any]:
    account_id = int(payload.get("account_id") or 0)
    keyword = str(payload.get("keyword") or "").strip()
    content = str(payload.get("content") or "").strip()
    if not account_id or not keyword or not content:
        raise ValueError("发货规则需要账号、关键词和发货内容")
    rule_id = execute(
        """
        INSERT INTO delivery_rules(
            account_id, keyword, content_type, content, auto_confirm, enabled,
            cooldown_seconds, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            keyword,
            str(payload.get("content_type") or "text"),
            content,
            1 if payload.get("auto_confirm") else 0,
            0 if payload.get("enabled") is False else 1,
            int(payload.get("cooldown_seconds") or 600),
            now_iso(),
        ),
    )
    audit("create_delivery_rule", "delivery_rule", rule_id, account_id, {"keyword": keyword})
    return row_to_dict(query_one("SELECT * FROM delivery_rules WHERE id = ?", (rule_id,)))


def simulate_delivery(payload: dict[str, Any]) -> dict[str, Any]:
    account_id = int(payload.get("account_id") or 0)
    message = str(payload.get("message") or payload.get("item_title") or "").strip()
    if not account_id or not message:
        raise ValueError("模拟发货需要账号和订单/商品信息")
    account = query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not account:
        raise ValueError("账号不存在")
    rules = query(
        """
        SELECT * FROM delivery_rules
        WHERE account_id = ? AND enabled = 1
        ORDER BY id ASC
        """,
        (account_id,),
    )
    rule = next((item for item in rules if str(item["keyword"]) in message), None)
    status = "queued"
    last_error = None
    if not rule:
        status = "paused"
        last_error = "未匹配发货规则"
    elif account["status"] != "active":
        status = "paused"
        last_error = "账号非 active 状态"
    elif not account["auto_delivery_enabled"]:
        status = "paused"
        last_error = "账号未开启自动发货"
    job_id = execute(
        """
        INSERT INTO delivery_jobs(
            account_id, rule_id, order_id, buyer_name, item_title, status,
            last_error, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            account_id,
            int(rule["id"]) if rule else None,
            str(payload.get("order_id") or f"order-{int(time.time())}"),
            str(payload.get("buyer_name") or "买家"),
            str(payload.get("item_title") or message),
            status,
            last_error,
            now_iso(),
            now_iso(),
        ),
    )
    audit("simulate_delivery", "delivery_job", job_id, account_id, {"status": status, "last_error": last_error})
    return row_to_dict(query_one("SELECT * FROM delivery_jobs WHERE id = ?", (job_id,)))


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def get_market_trends(params: dict[str, list[str]]) -> dict[str, Any]:
    keyword = (params.get("keyword", [""])[0] or "").strip()
    item_id = (params.get("item_id", [""])[0] or "").strip()
    days = max(1, min(_parse_int(params.get("days", ["30"])[0], 30), 365))
    bucket = (params.get("bucket", ["day"])[0] or "day").strip()
    bucket_expr = "strftime('%Y-%m-%d %H:00', observed_at)" if bucket == "hour" else "date(observed_at)"
    where = ["observed_at >= datetime('now', ?)"]
    values: list[Any] = [f"-{days} days"]
    if keyword:
        where.append("keyword LIKE ?")
        values.append(f"%{keyword}%")
    if item_id:
        where.append("item_id = ?")
        values.append(item_id)
    where_sql = " AND ".join(where)
    buckets = rows_to_dicts(
        query(
            f"""
            WITH base AS (
                SELECT *, {bucket_expr} AS bucket
                FROM market_snapshots
                WHERE {where_sql}
            ),
            bucket_stats AS (
                SELECT
                    bucket,
                    COUNT(*) AS samples,
                    COUNT(DISTINCT item_id) AS item_count,
                    ROUND(AVG(price), 2) AS avg_price,
                    MIN(price) AS min_price,
                    MAX(price) AS max_price,
                    ROUND(AVG(want_count), 2) AS avg_want_count,
                    ROUND(AVG(browse_count), 2) AS avg_browse_count
                FROM base
                GROUP BY bucket
            ),
            item_delta AS (
                SELECT
                    bucket,
                    item_id,
                    MAX(sold_count) - MIN(sold_count) AS sold_delta,
                    MAX(sales_volume) - MIN(sales_volume) AS sales_delta
                FROM base
                GROUP BY bucket, item_id
            )
            SELECT
                bucket_stats.*,
                COALESCE(SUM(item_delta.sold_delta), 0) AS sold_delta,
                COALESCE(SUM(item_delta.sales_delta), 0) AS sales_delta
            FROM bucket_stats
            LEFT JOIN item_delta ON item_delta.bucket = bucket_stats.bucket
            GROUP BY bucket_stats.bucket
            ORDER BY bucket_stats.bucket
            """,
            tuple(values),
        )
    )
    item_rows = rows_to_dicts(
        query(
            f"""
            SELECT
                item_id,
                COALESCE(MAX(title), item_id) AS title,
                COUNT(*) AS samples,
                ROUND(AVG(price), 2) AS avg_price,
                MIN(price) AS min_price,
                MAX(price) AS max_price,
                MAX(price) - MIN(price) AS price_range,
                MAX(want_count) - MIN(want_count) AS want_delta,
                MAX(browse_count) - MIN(browse_count) AS browse_delta,
                MAX(sold_count) - MIN(sold_count) AS sold_delta,
                MAX(sales_volume) - MIN(sales_volume) AS sales_delta,
                MIN(rank) AS best_rank,
                MAX(observed_at) AS last_observed_at
            FROM market_snapshots
            WHERE {where_sql}
            GROUP BY item_id
            ORDER BY sales_delta DESC, sold_delta DESC, want_delta DESC, browse_delta DESC, samples DESC
            LIMIT 20
            """,
            tuple(values),
        )
    )
    latest_keywords = rows_to_dicts(
        query(
            """
            SELECT keyword, COUNT(*) AS samples, MAX(observed_at) AS last_observed_at
            FROM market_snapshots
            WHERE keyword != ''
            GROUP BY keyword
            ORDER BY last_observed_at DESC
            LIMIT 20
            """
        )
    )
    totals = query_one(
        f"""
        SELECT
            COUNT(*) AS samples,
            COUNT(DISTINCT item_id) AS item_count,
            ROUND(AVG(price), 2) AS avg_price,
            MIN(price) AS min_price,
            MAX(price) AS max_price,
            SUM(CASE WHEN sold_count > 0 OR sales_volume > 0 THEN 1 ELSE 0 END) AS rows_with_sales
        FROM market_snapshots
        WHERE {where_sql}
        """,
        tuple(values),
    )
    return {
        "filters": {"keyword": keyword, "item_id": item_id, "days": days, "bucket": bucket},
        "summary": row_to_dict(totals) or {},
        "buckets": buckets,
        "items": item_rows,
        "keywords": latest_keywords,
        "notes": [
            "趋势基于本地历史采集快照；时间越久、采集越稳定，趋势越可靠。",
            "销量字段只使用平台接口返回的成交/销量信息；如果接口没有暴露，则用想要数、浏览数、搜索排名变化作为热度代理。",
        ],
    }


def get_summary() -> dict[str, Any]:
    def count(table: str, where: str = "1=1") -> int:
        row = query_one(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")
        return int(row["n"] if row else 0)

    prices = query("SELECT price FROM items ORDER BY observed_at DESC LIMIT 200")
    avg_price = round(sum(float(row["price"]) for row in prices) / len(prices), 2) if prices else 0
    return {
        "accounts": count("accounts"),
        "active_accounts": count("accounts", "status = 'active'"),
        "items": count("items"),
        "market_snapshots": count("market_snapshots"),
        "publish_jobs": count("publish_jobs"),
        "queued_publish_jobs": count("publish_jobs", "status = 'queued'"),
        "messages": count("messages"),
        "delivery_jobs": count("delivery_jobs"),
        "risk_events": count("risk_events"),
        "avg_price": avg_price,
        "global_kill_switch": get_setting("global_kill_switch", "false") == "true",
        "adapter": ADAPTER.name,
    }


def process_publish_job(row: sqlite3.Row) -> None:
    job_id = int(row["id"])
    execute(
        "UPDATE publish_jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE id = ?",
        (now_iso(), job_id),
    )
    account = query_one("SELECT * FROM accounts WHERE id = ?", (int(row["account_id"]),))
    draft = query_one("SELECT * FROM publish_drafts WHERE id = ?", (int(row["draft_id"]),))
    try:
        if get_setting("global_kill_switch", "false") == "true":
            raise RuntimeError("全局 kill switch 已开启")
        if not account or not draft:
            raise RuntimeError("账号或草稿不存在")
        if account["status"] != "active":
            raise RuntimeError(f"账号状态为 {account['status']}，任务暂停")
        if row["mode"] == "auto" and not account["auto_publish_enabled"]:
            raise RuntimeError("账号未开启全自动发布")
        if int(account["published_today"]) >= int(account["daily_publish_limit"]):
            raise RuntimeError("达到账号每日发布上限")
        result = ADAPTER.publish_item(adapter_account(account), row_to_dict(draft) or {})
        execute(
            """
            UPDATE publish_jobs
            SET status = 'success', result_item_id = ?, last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (result["item_id"], now_iso(), job_id),
        )
        execute("UPDATE publish_drafts SET status = 'published' WHERE id = ?", (int(row["draft_id"]),))
        execute("UPDATE accounts SET published_today = published_today + 1 WHERE id = ?", (int(row["account_id"]),))
        audit("publish_success", "publish_job", job_id, int(row["account_id"]), result)
    except Exception as exc:
        message = str(exc)
        status = "paused" if "风控" in message or "kill switch" in message or "账号状态" in message or "上限" in message else "failed"
        execute(
            "UPDATE publish_jobs SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (status, message, now_iso(), job_id),
        )
        if "风控" in message or isinstance(exc, RiskDetectedError):
            add_risk_event(int(row["account_id"]), "high", "publish_risk", message, "pause_account_and_job")
        audit("publish_failed", "publish_job", job_id, int(row["account_id"]), {"status": status, "error": message})


def process_delivery_job(row: sqlite3.Row) -> None:
    job_id = int(row["id"])
    execute(
        "UPDATE delivery_jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE id = ?",
        (now_iso(), job_id),
    )
    account = query_one("SELECT * FROM accounts WHERE id = ?", (int(row["account_id"]),))
    rule = query_one("SELECT * FROM delivery_rules WHERE id = ?", (int(row["rule_id"]),)) if row["rule_id"] else None
    try:
        if get_setting("global_kill_switch", "false") == "true":
            raise RuntimeError("全局 kill switch 已开启")
        if not account or not rule:
            raise RuntimeError("账号或发货规则不存在")
        if account["status"] != "active":
            raise RuntimeError(f"账号状态为 {account['status']}，任务暂停")
        if not account["auto_delivery_enabled"]:
            raise RuntimeError("账号未开启自动发货")
        result = ADAPTER.deliver(adapter_account(account), str(row["order_id"]), str(rule["content"]), bool(rule["auto_confirm"]))
        execute(
            """
            UPDATE delivery_jobs
            SET status = 'success', confirm_status = ?, content_sent = ?, last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (result["confirm_status"], rule["content"], now_iso(), job_id),
        )
        execute("UPDATE delivery_rules SET last_sent_at = ? WHERE id = ?", (now_iso(), int(rule["id"])))
        audit("delivery_success", "delivery_job", job_id, int(row["account_id"]), result)
    except Exception as exc:
        message = str(exc)
        status = "paused" if "风控" in message or "kill switch" in message or "账号状态" in message else "failed"
        execute(
            "UPDATE delivery_jobs SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (status, message, now_iso(), job_id),
        )
        if "风控" in message or isinstance(exc, RiskDetectedError):
            add_risk_event(int(row["account_id"]), "high", "delivery_risk", message, "pause_account_and_job")
        audit("delivery_failed", "delivery_job", job_id, int(row["account_id"]), {"status": status, "error": message})


def worker_loop() -> None:
    init_db()
    while not STOP_EVENT.is_set():
        try:
            publish_job = query_one(
                """
                SELECT * FROM publish_jobs
                WHERE status = 'queued' AND scheduled_at <= ?
                ORDER BY scheduled_at ASC, id ASC
                LIMIT 1
                """,
                (now_iso(),),
            )
            if publish_job:
                process_publish_job(publish_job)
            delivery_job = query_one(
                """
                SELECT * FROM delivery_jobs
                WHERE status = 'queued'
                ORDER BY id ASC
                LIMIT 1
                """
            )
            if delivery_job:
                process_delivery_job(delivery_job)
        except Exception:
            traceback.print_exc()
        STOP_EVENT.wait(0.8)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "XianyuOpsWorkbench/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[%s] %s\n" % (now_iso(), fmt % args))

    def send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json({"ok": False, "error": message}, status)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def do_GET(self) -> None:
        try:
            self.route_get()
        except Exception as exc:
            self.send_error_json(500, str(exc))

    def do_POST(self) -> None:
        try:
            self.route_write("POST")
        except ValueError as exc:
            self.send_error_json(400, str(exc))
        except Exception as exc:
            traceback.print_exc()
            self.send_error_json(500, str(exc))

    def do_PATCH(self) -> None:
        try:
            self.route_write("PATCH")
        except ValueError as exc:
            self.send_error_json(400, str(exc))
        except Exception as exc:
            traceback.print_exc()
            self.send_error_json(500, str(exc))

    def route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/api/summary":
            return self.send_json(get_summary())
        if path == "/api/settings":
            return self.send_json({"global_kill_switch": get_setting("global_kill_switch", "false") == "true"})
        if path == "/api/accounts":
            return self.send_json([public_account(row) for row in query("SELECT * FROM accounts ORDER BY id DESC")])
        if path == "/api/items":
            limit = int(qs.get("limit", ["80"])[0])
            return self.send_json(rows_to_dicts(query("SELECT * FROM items ORDER BY observed_at DESC LIMIT ?", (limit,))))
        if path == "/api/trends":
            return self.send_json(get_market_trends(qs))
        if path == "/api/publish-drafts":
            return self.send_json(rows_to_dicts(query("SELECT * FROM publish_drafts ORDER BY id DESC LIMIT 80")))
        if path == "/api/publish-jobs":
            return self.send_json(rows_to_dicts(query("SELECT * FROM publish_jobs ORDER BY id DESC LIMIT 80")))
        if path == "/api/reply-rules":
            return self.send_json(rows_to_dicts(query("SELECT * FROM reply_rules ORDER BY priority DESC, id DESC")))
        if path == "/api/messages":
            return self.send_json(rows_to_dicts(query("SELECT * FROM messages ORDER BY id DESC LIMIT 80")))
        if path == "/api/delivery-rules":
            return self.send_json(rows_to_dicts(query("SELECT * FROM delivery_rules ORDER BY id DESC")))
        if path == "/api/delivery-jobs":
            return self.send_json(rows_to_dicts(query("SELECT * FROM delivery_jobs ORDER BY id DESC LIMIT 80")))
        if path == "/api/risk-events":
            return self.send_json(rows_to_dicts(query("SELECT * FROM risk_events ORDER BY id DESC LIMIT 100")))
        if path == "/api/audit-logs":
            return self.send_json(rows_to_dicts(query("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 120")))
        self.serve_static(path)

    def route_write(self, method: str) -> None:
        path = urlparse(self.path).path
        payload = self.read_json()
        if method == "POST" and path == "/api/seed":
            return self.send_json(seed_demo())
        if method == "PATCH" and path == "/api/settings":
            set_setting("global_kill_switch", "true" if payload.get("global_kill_switch") else "false")
            return self.send_json({"global_kill_switch": get_setting("global_kill_switch") == "true"})
        if method == "POST" and path == "/api/accounts":
            return self.send_json(create_account(payload), 201)
        match = re.fullmatch(r"/api/accounts/(\d+)", path)
        if method == "PATCH" and match:
            return self.send_json(update_account(int(match.group(1)), payload))
        match = re.fullmatch(r"/api/accounts/(\d+)/login-capture/start", path)
        if method == "POST" and match:
            return self.send_json(start_login_capture(int(match.group(1))))
        match = re.fullmatch(r"/api/login-capture/([0-9a-f]+)/status", path)
        if method == "POST" and match:
            return self.send_json(get_login_capture(match.group(1)))
        match = re.fullmatch(r"/api/login-capture/([0-9a-f]+)/goto", path)
        if method == "POST" and match:
            return self.send_json(goto_login_capture(match.group(1), str(payload.get("url") or "")))
        match = re.fullmatch(r"/api/login-capture/([0-9a-f]+)/save", path)
        if method == "POST" and match:
            return self.send_json(save_login_capture(match.group(1)))
        match = re.fullmatch(r"/api/login-capture/([0-9a-f]+)/close", path)
        if method == "POST" and match:
            return self.send_json(close_login_capture(match.group(1)))
        if method == "POST" and path == "/api/collector/run":
            account_id = int(payload["account_id"]) if payload.get("account_id") else None
            created = run_collection(str(payload.get("keyword") or "相机"), int(payload.get("limit") or 6), account_id)
            return self.send_json({"ok": True, "items_created": created})
        if method == "POST" and path == "/api/publish-drafts":
            return self.send_json(create_publish_draft(payload), 201)
        if method == "POST" and path == "/api/publish-jobs":
            return self.send_json(create_publish_job(payload), 201)
        match = re.fullmatch(r"/api/publish-jobs/(\d+)/confirm", path)
        if method == "POST" and match:
            return self.send_json(confirm_publish_job(int(match.group(1))))
        match = re.fullmatch(r"/api/publish-jobs/(\d+)/pause", path)
        if method == "POST" and match:
            return self.send_json(pause_publish_job(int(match.group(1))))
        if method == "POST" and path == "/api/reply-rules":
            return self.send_json(create_reply_rule(payload), 201)
        if method == "POST" and path == "/api/messages/simulate":
            return self.send_json(simulate_message(payload), 201)
        if method == "POST" and path == "/api/delivery-rules":
            return self.send_json(create_delivery_rule(payload), 201)
        if method == "POST" and path == "/api/delivery-jobs/simulate":
            return self.send_json(simulate_delivery(payload), 201)
        self.send_error_json(404, "接口不存在")

    def serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            file_path = STATIC_DIR / "index.html"
        elif path.startswith("/static/"):
            file_path = STATIC_DIR / path.removeprefix("/static/")
        else:
            self.send_error_json(404, "页面不存在")
            return
        file_path = file_path.resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.exists():
            self.send_error_json(404, "静态文件不存在")
            return
        suffix = file_path.suffix.lower()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(suffix, "application/octet-stream")
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    init_db()
    worker = threading.Thread(target=worker_loop, name="job-worker", daemon=True)
    worker.start()
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"Xianyu Ops Workbench running at http://{HOST}:{PORT} (adapter={ADAPTER.name})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STOP_EVENT.set()
        server.server_close()


if __name__ == "__main__":
    main()
