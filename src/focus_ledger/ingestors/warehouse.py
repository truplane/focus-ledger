"""Data warehouse query-history CSV/JSON ingestor (Snowflake, Databricks)."""

from __future__ import annotations

import csv
import json
from datetime import timedelta
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


def _load_warehouse_rows(input_path: Path) -> list[dict[str, Any]]:
    """Read warehouse query history from CSV or JSON/JSONL files."""
    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        with input_path.open("r", encoding="utf-8", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    if suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        with input_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
        return rows
    with input_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "queries" in data:
        return list(data["queries"])
    if isinstance(data, list):
        return list(data)
    raise ValueError(f"Unsupported warehouse JSON payload structure in {input_path}")


@register_ingestor
class WarehouseIngestor(BaseIngestor):
    """Normalizes Snowflake & Databricks query-history spend into FOCUS 1.5."""

    source_name = "warehouse"

    def parse(self, input_path: Path, pricing: PricingBook) -> list[FocusRecord]:
        rows = _load_warehouse_rows(input_path)
        records: list[FocusRecord] = []

        for idx, row in enumerate(rows):
            platform = str(row.get("platform") or row.get("provider") or "snowflake").lower()
            warehouse_name = str(
                row.get("warehouse_name") or row.get("cluster_name") or "DEFAULT_WH"
            )
            query_id = str(row.get("query_id") or row.get("statement_id") or f"q-{idx}")
            user_name = str(row.get("user_name") or row.get("executed_by") or "service-account")

            credits_used = float(
                row.get("credits_used")
                or row.get("dbu_consumed")
                or row.get("credits")
                or 0.0
            )
            credit_rate = pricing.get_warehouse_credit_usd(platform)
            if row.get("cost_usd") not in (None, ""):
                effective_cost = float(row["cost_usd"])
            else:
                effective_cost = round(credits_used * credit_rate, 8)

            mapped = pricing.get_warehouse_cost_center(warehouse_name)
            default_team = mapped[0] if mapped else "data-platform"
            default_project = mapped[1] if mapped else warehouse_name.lower()

            team = str(row.get("team") or default_team)
            project_id = str(row.get("project_id") or default_project)

            start_ts = parse_timestamp(row.get("start_time") or row.get("timestamp"))
            exec_ms = float(row.get("execution_time_ms") or row.get("duration_ms") or 1000.0)
            end_ts = (
                parse_timestamp(row.get("end_time"))
                if row.get("end_time")
                else start_ts + timedelta(milliseconds=exec_ms)
            )
            bill_start, bill_end = month_bounds(start_ts)

            provider_display = "Snowflake" if "snow" in platform else (
                "Databricks" if "data" in platform else platform.title()
            )
            unit_name = "DBUs" if provider_display == "Databricks" else "Credits"
            unit_price = round(effective_cost / credits_used, 8) if credits_used > 0 else credit_rate
            region = str(row.get("region") or "us-west-2")

            tags = {
                "warehouse": warehouse_name,
                "user": user_name,
                "team": team,
                "project_id": project_id,
                "platform": provider_display,
            }

            records.append(
                FocusRecord(
                    BillingAccountId=f"wh-{platform}",
                    BillingAccountName=f"{provider_display} Data Warehouse",
                    BillingCurrency=pricing.currency,
                    BillingPeriodStart=bill_start,
                    BillingPeriodEnd=bill_end,
                    ChargeCategory="Usage",
                    ChargeClass="Regular",
                    ChargeDescription=(
                        f"{provider_display} query {query_id} on {warehouse_name} "
                        f"({credits_used:.4f} {unit_name})"
                    ),
                    ChargeFrequency="Usage-Based",
                    ChargePeriodStart=start_ts,
                    ChargePeriodEnd=end_ts,
                    ConsumedQuantity=credits_used,
                    ConsumedUnit=unit_name,
                    ContractedCost=effective_cost,
                    ContractedUnitPrice=unit_price,
                    EffectiveCost=effective_cost,
                    InvoiceIssuerName=provider_display,
                    ListCost=effective_cost,
                    ListUnitPrice=unit_price,
                    PricingCategory="Standard",
                    PricingQuantity=credits_used,
                    PricingUnit=unit_name,
                    ProviderName=provider_display,
                    PublisherName=provider_display,
                    RegionId=region,
                    RegionName=region,
                    ResourceId=query_id,
                    ResourceName=warehouse_name,
                    ResourceType="Data Warehouse Query",
                    ServiceCategory="Analytics",
                    ServiceName=f"{provider_display} SQL Compute",
                    SkuId=f"{platform}:{warehouse_name}",
                    SkuPriceId=f"{platform}:compute-credit",
                    SubAccountId=warehouse_name,
                    SubAccountName=f"{team}/{warehouse_name}",
                    Tags=tags,
                    x_record_id=deterministic_id(self.source_name, query_id, str(idx)),
                    x_source=self.source_name,
                    x_team=team,
                    x_project_id=project_id,
                    x_model="n/a",
                    x_caller=user_name,
                    x_agent_id=warehouse_name,
                )
            )
        return records
