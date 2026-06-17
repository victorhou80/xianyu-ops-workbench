from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AdapterActionError(RuntimeError):
    """Base exception for adapter-level failures."""


class RiskDetectedError(AdapterActionError):
    """Raised when an adapter detects a platform risk/challenge state."""


class NotConfiguredError(AdapterActionError):
    """Raised when a real adapter method has not been configured yet."""


class GoofishAdapter(ABC):
    name = "base"

    @abstractmethod
    def search_items(self, keyword: str, limit: int, account: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return normalized item dicts for collection/statistics."""

    @abstractmethod
    def publish_item(self, account: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
        """Publish one draft and return at least item_id/detail_url."""

    @abstractmethod
    def send_reply(self, account: dict[str, Any], chat_id: str, text: str) -> dict[str, Any]:
        """Send one IM reply and return a message identifier."""

    @abstractmethod
    def deliver(self, account: dict[str, Any], order_id: str, content: str, auto_confirm: bool) -> dict[str, Any]:
        """Send delivery content and optionally confirm shipment."""
