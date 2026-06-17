from __future__ import annotations

from .base import GoofishAdapter, AdapterActionError, NotConfiguredError, RiskDetectedError
from .mock_goofish import MockGoofishAdapter
from .real_goofish import RealGoofishAdapter


def build_adapter(mode: str) -> GoofishAdapter:
    normalized = (mode or "mock").strip().lower()
    if normalized == "mock":
        return MockGoofishAdapter()
    if normalized == "real":
        return RealGoofishAdapter()
    raise ValueError(f"Unknown adapter mode: {mode}")


__all__ = [
    "AdapterActionError",
    "GoofishAdapter",
    "MockGoofishAdapter",
    "NotConfiguredError",
    "RealGoofishAdapter",
    "RiskDetectedError",
    "build_adapter",
]
