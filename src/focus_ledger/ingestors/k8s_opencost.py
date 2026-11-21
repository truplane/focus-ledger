"""Kubernetes OpenCost / Prometheus allocation JSON ingestor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from focus_ledger.ingestors.base import (
    BaseIngestor,
    deterministic_id,
    month_bounds,
    parse_timestamp,
    register_ingestor,
)
from focus_ledger.pricing import PricingBook
from focus_ledger.schema import FocusRecord


def _extract_allocations(payload: Any) -> list[dict[str, Any]]:
    """Support OpenCost /allocation/compute arrays, dicts, and custom list payloads."""
    if isinstance(payload, list):
        items: list[dict[str, Any]] = []
        for elem in payload:
            items.extend(_extract_allocations(elem))
        return items
    if isinstance(payload, dict):
        if "data" in payload:
            return _extract_allocations(payload["data"])
        if "allocations" in payload and isinstance(payload["allocations"], list):
            return [a for a in payload["allocations"] if isinstance(a, dict)]
        # OpenCost maps workload key -> allocation object
        if "name" in payload or "properties" in payload or "cpuCoreHours" in payload:
            return [payload]
        collected: list[dict[str, Any]] = []
        for key, val in payload.items():
            if isinstance(val, dict):
                merged = dict(val)
                merged.setdefault("name", key)
                collected.append(merged)
        return collected
    return []


@register_ingestor
class K8sOpenCostIngestor(BaseIngestor):
    """Normalizes OpenCost namespace/workload allocation JSON payloads into FOCUS 1.5."""

    source_name = "k8s-opencost"

    def parse(self, input_path: Path, pricing: PricingBook) -> list[FocusRecord]:
        with input_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)

        allocations = _extract_allocations(payload)
        records: list[FocusRecord] = []

        cpu_rate = pricing.kubernetes.get("cpu_core_hour_usd", 0.031611)
        ram_gb_rate = pricing.kubernetes.get("ram_gb_hour_usd", 0.004237)
        pv_gb_rate = pricing.kubernetes.get("pv_gb_hour_usd", 0.00014)

        for idx, alloc in enumerate(allocations):
            props = alloc.get("properties") if isinstance(alloc.get("properties"), dict) else {}
            labels = props.get("labels") if isinstance(props.get("labels"), dict) else {}
            if isinstance(alloc.get("labels"), dict):
                labels = {**alloc["labels"], **labels}

            name = str(alloc.get("name") or f"workload-{idx}")
            namespace = str(props.get("namespace") or alloc.get("namespace") or "default")
            cluster = str(props.get("cluster") or alloc.get("cluster") or "prod-cluster")
            controller = str(props.get("controller") or alloc.get("controller") or name)
            container = str(props.get("container") or "")

            window = alloc.get("window") if isinstance(alloc.get("window"), dict) else {}
            start_ts = parse_timestamp(window.get("start") or alloc.get("start"))
            end_ts = parse_timestamp(window.get("end") or alloc.get("end"), default=start_ts)
            if end_ts <= start_ts:
                end_ts = start_ts
            bill_start, bill_end = month_bounds(start_ts)

            cpu_hours = float(alloc.get("cpuCoreHours") or 0.0)
            ram_gb_hours = float(alloc.get("ramByteHours") or 0.0) / (1024.0**3)
            pv_gb_hours = float(alloc.get("pvByteHours") or 0.0) / (1024.0**3)

            if "totalCost" in alloc and alloc["totalCost"] is not None:
                effective_cost = float(alloc["totalCost"])
            else:
                cpu_cost = float(alloc.get("cpuCost") or (cpu_hours * cpu_rate))
                ram_cost = float(alloc.get("ramCost") or (ram_gb_hours * ram_gb_rate))
                pv_cost = float(alloc.get("pvCost") or (pv_gb_hours * pv_gb_rate))
                net_cost = float(alloc.get("networkCost") or 0.0)
                gpu_cost = float(alloc.get("gpuCost") or 0.0)
                effective_cost = round(cpu_cost + ram_cost + pv_cost + net_cost + gpu_cost, 8)

            team = str(
                labels.get("team")
                or labels.get("owner")
                or alloc.get("team")
                or namespace
            )
            project_id = str(
                labels.get("project_id")
                or labels.get("app.kubernetes.io/part-of")
                or labels.get("app")
                or namespace
            )

            consumed_qty = round(cpu_hours if cpu_hours > 0 else max(ram_gb_hours, 1.0), 6)
            unit_price = round(effective_cost / consumed_qty, 8) if consumed_qty > 0 else 0.0
            resource_id = f"{cluster}/{namespace}/{controller}" + (
                f"/{container}" if container else ""
            )

            tags = {str(k): str(v) for k, v in labels.items()}
            tags.update({"namespace": namespace, "cluster": cluster, "controller": controller})

            records.append(
                FocusRecord(
                    BillingAccountId=f"k8s-{cluster}",
                    BillingAccountName=f"Kubernetes ({cluster})",
                    BillingCurrency=pricing.currency,
                    BillingPeriodStart=bill_start,
                    BillingPeriodEnd=bill_end,
                    ChargeCategory="Usage",
                    ChargeClass="Regular",
                    ChargeDescription=(
                        f"K8s allocation {namespace}/{controller} "
                        f"(cpuCoreHours={cpu_hours:.2f}, ramGBHours={ram_gb_hours:.2f})"
                    ),
                    ChargeFrequency="Usage-Based",
                    ChargePeriodStart=start_ts,
                    ChargePeriodEnd=end_ts,
                    ConsumedQuantity=consumed_qty,
                    ConsumedUnit="CPU Core-Hours",
                    ContractedCost=effective_cost,
                    ContractedUnitPrice=unit_price,
                    EffectiveCost=effective_cost,
                    InvoiceIssuerName="Kubernetes",
                    ListCost=effective_cost,
                    ListUnitPrice=unit_price,
                    PricingCategory="Standard",
                    PricingQuantity=consumed_qty,
                    PricingUnit="CPU Core-Hours",
                    ProviderName="Kubernetes",
                    PublisherName="OpenCost",
                    RegionId=cluster,
                    RegionName=cluster,
                    ResourceId=resource_id,
                    ResourceName=controller,
                    ResourceType="Kubernetes Workload",
                    ServiceCategory="Compute",
                    ServiceName="Kubernetes Compute",
                    SkuId=f"k8s:{namespace}:{controller}",
                    SkuPriceId="k8s:opencost:allocation",
                    SubAccountId=namespace,
                    SubAccountName=f"{cluster}/{namespace}",
                    Tags=tags,
                    x_record_id=deterministic_id(
                        self.source_name, resource_id, start_ts.isoformat(), str(idx)
                    ),
                    x_source=self.source_name,
                    x_team=team,
                    x_project_id=project_id,
                    x_model="n/a",
                    x_caller=controller,
                    x_agent_id=namespace,
                )
            )
        return records
