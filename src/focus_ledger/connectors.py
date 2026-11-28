"""Real-time pull connectors for OpenCost and cloud data warehouse APIs."""

from __future__ import annotations

import json
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Callable

from focus_ledger.ingestors.k8s_opencost import K8sOpenCostIngestor
from focus_ledger.ingestors.warehouse import WarehouseIngestor
from focus_ledger.pricing import PricingBook
from focus_ledger.storage import FocusStorage

JSONFetcher = Callable[[str], Any]


def _default_json_fetch(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


class OpenCostPullConnector:
    """Polls OpenCost /allocation/compute HTTP endpoint and ingests FOCUS 1.5 records."""

    def __init__(
        self,
        base_url: str,
        storage: FocusStorage,
        pricing: PricingBook | None = None,
        window: str = "1h",
        aggregate: str = "namespace",
        fetcher: JSONFetcher | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.storage = storage
        self.pricing = pricing or PricingBook.load()
        self.window = window
        self.aggregate = aggregate
        self.fetcher = fetcher or _default_json_fetch
        self._ingestor = K8sOpenCostIngestor()

    def poll_once(self) -> int:
        """Fetch live OpenCost allocation payload and upsert into DuckDB + Parquet."""
        endpoint = (
            f"{self.base_url}/allocation/compute"
            f"?window={self.window}&aggregate={self.aggregate}"
        )
        payload = self.fetcher(endpoint)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(payload, tmp)
            tmp_path = Path(tmp.name)
        try:
            records = self._ingestor.parse(tmp_path, self.pricing)
            return self.storage.insert_records(records, write_parquet=True)
        finally:
            tmp_path.unlink(missing_ok=True)


class WarehousePullConnector:
    """Polls Snowflake/Databricks query-history rows via an HTTP or SQL fetcher."""

    def __init__(
        self,
        endpoint_or_dsn: str,
        storage: FocusStorage,
        pricing: PricingBook | None = None,
        fetcher: JSONFetcher | None = None,
    ) -> None:
        self.endpoint_or_dsn = endpoint_or_dsn
        self.storage = storage
        self.pricing = pricing or PricingBook.load()
        self.fetcher = fetcher or _default_json_fetch
        self._ingestor = WarehouseIngestor()

    def poll_once(self) -> int:
        payload = self.fetcher(self.endpoint_or_dsn)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(payload, tmp)
            tmp_path = Path(tmp.name)
        try:
            records = self._ingestor.parse(tmp_path, self.pricing)
            return self.storage.insert_records(records, write_parquet=True)
        finally:
            tmp_path.unlink(missing_ok=True)
