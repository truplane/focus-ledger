"""Base ingestor interface, registry, and shared timestamp/hash helpers."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from focus_ledger.pricing import PricingBook
from focus_ledger.schema import FocusRecord


class BaseIngestor(ABC):
    """Abstract base class for normalizing telemetry into FOCUS 1.5 records."""

    source_name: str = ""

    @abstractmethod
    def parse(self, input_path: Path, pricing: PricingBook) -> list[FocusRecord]:
        """Parse an input telemetry artifact into canonical FocusRecord entries."""


_REGISTRY: dict[str, type[BaseIngestor]] = {}


def register_ingestor(cls: type[BaseIngestor]) -> type[BaseIngestor]:
    """Decorator registering an ingestor class by its source_name."""
    if not cls.source_name:
        raise ValueError(f"Ingestor {cls.__name__} must define source_name")
    _REGISTRY[cls.source_name] = cls
    return cls


def get_ingestor(source: str) -> BaseIngestor:
    """Instantiate a registered ingestor for the given source key."""
    key = source.strip().lower()
    if key not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"Unknown source '{source}'. Supported sources: {available}")
    return _REGISTRY[key]()


def list_sources() -> list[str]:
    """Return sorted names of all registered ingestor sources."""
    return sorted(_REGISTRY)


def parse_timestamp(value: str | int | float | None, default: datetime | None = None) -> datetime:
    """Parse ISO-8601 strings or Unix timestamps into UTC-aware datetimes."""
    if value is None or value == "":
        return default or datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def month_bounds(ts: datetime) -> tuple[datetime, datetime]:
    """Compute UTC billing period [start, end) for the timestamp's calendar month."""
    start = ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def deterministic_id(*parts: str) -> str:
    """Create a stable SHA-256 prefix identifier for deduplicating ingested rows."""
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]
