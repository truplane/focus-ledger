"""Pluggable ingestor registry for Focus Ledger."""

from focus_ledger.ingestors.ai_gateway import AIGatewayIngestor
from focus_ledger.ingestors.base import BaseIngestor, get_ingestor, list_sources, register_ingestor
from focus_ledger.ingestors.k8s_opencost import K8sOpenCostIngestor
from focus_ledger.ingestors.warehouse import WarehouseIngestor

__all__ = [
    "AIGatewayIngestor",
    "BaseIngestor",
    "K8sOpenCostIngestor",
    "WarehouseIngestor",
    "get_ingestor",
    "list_sources",
    "register_ingestor",
]
