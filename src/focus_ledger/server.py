"""FastAPI server exposing FOCUS 1.5 health, Prometheus metrics, SKU pricing, and cross-tool events."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from pydantic import BaseModel, Field

from focus_ledger.anomalies import detect_spend_anomalies
from focus_ledger.ingestors.base import deterministic_id, month_bounds
from focus_ledger.pricing import PricingBook
from focus_ledger.schema import FocusRecord
from focus_ledger.storage import FocusStorage


class RightsizingSavingsEvent(BaseModel):
    """Verified rightsizing savings event emitted by k8s-safescale after bake success."""

    namespace: str = "default"
    policy: str
    workload: str
    team: str = "platform"
    project_id: str = "default"
    cpu_milli_saved: int = 0
    memory_mib_saved: int = 0
    monthly_savings_usd: float = Field(ge=0.0)


class DataQualityCostEvent(BaseModel):
    """Data quality regression / reprocessing cost event emitted by data-snap."""

    pipeline: str
    dataset: str
    team: str = "data-platform"
    project_id: str = "default"
    failed_checks: int = Field(ge=0, default=1)
    reprocessing_cost_usd: float = Field(ge=0.0)


def build_metrics_payload(storage: FocusStorage) -> bytes:
    """Build a fresh Prometheus exposition payload from DuckDB state."""
    registry = CollectorRegistry()

    cost_gauge = Gauge(
        "focus_ledger_effective_cost_usd",
        "Normalized FOCUS 1.5 effective cost in USD",
        ["team", "project", "model", "source", "service"],
        registry=registry,
    )
    qty_gauge = Gauge(
        "focus_ledger_consumed_quantity_total",
        "Consumed resource quantity normalized into FOCUS 1.5",
        ["team", "project", "model", "source", "unit"],
        registry=registry,
    )
    tokens_gauge = Gauge(
        "focus_ledger_ai_tokens_total",
        "AI Gateway tokens attributed by team, project, model, and token type",
        ["team", "project", "model", "token_type"],
        registry=registry,
    )
    records_gauge = Gauge(
        "focus_ledger_records_total",
        "Total ingested FOCUS 1.5 records by source",
        ["source"],
        registry=registry,
    )
    total_cost_gauge = Gauge(
        "focus_ledger_total_effective_cost_usd",
        "Grand total FOCUS 1.5 effective cost in USD across all sources",
        registry=registry,
    )
    savings_gauge = Gauge(
        "focus_ledger_rightsizing_savings_usd",
        "Verified monthly rightsizing savings attributed from k8s-safescale",
        ["team", "project"],
        registry=registry,
    )
    quality_cost_gauge = Gauge(
        "focus_ledger_data_quality_cost_usd",
        "Attributed data quality regression reprocessing cost from data-snap",
        ["team", "project"],
        registry=registry,
    )
    anomaly_gauge = Gauge(
        "focus_ledger_spend_anomaly_zscore",
        "Latest 3-sigma spend anomaly z-score by source and team",
        ["source", "team"],
        registry=registry,
    )

    breakdown = storage.metrics_breakdown()
    grand_total = 0.0
    for team, project, model, source, service, cost in breakdown["cost"]:
        val = float(cost or 0.0)
        if str(source) == "k8s-safescale":
            savings_gauge.labels(team=str(team), project=str(project)).set(val)
        elif str(source) == "data-snap":
            quality_cost_gauge.labels(team=str(team), project=str(project)).set(val)
            grand_total += val
        else:
            grand_total += val
        cost_gauge.labels(
            team=str(team),
            project=str(project),
            model=str(model),
            source=str(source),
            service=str(service),
        ).set(val)

    for team, project, model, source, unit, qty in breakdown["quantity"]:
        qty_gauge.labels(
            team=str(team),
            project=str(project),
            model=str(model),
            source=str(source),
            unit=str(unit),
        ).set(float(qty or 0.0))

    for team, project, model, in_tok, out_tok, cached_tok in breakdown["tokens"]:
        for tok_type, count in (
            ("input", in_tok),
            ("output", out_tok),
            ("cached", cached_tok),
        ):
            tokens_gauge.labels(
                team=str(team),
                project=str(project),
                model=str(model),
                token_type=tok_type,
            ).set(float(count or 0))

    for source, count in breakdown["records"]:
        records_gauge.labels(source=str(source)).set(float(count or 0))

    for anom in detect_spend_anomalies(storage, z_threshold=3.0):
        anomaly_gauge.labels(source=anom["source"], team=anom["team"]).set(float(anom["z_score"]))

    total_cost_gauge.set(round(grand_total, 8))
    return generate_latest(registry)


def create_app(
    db_path: Path | str = "focus_ledger.duckdb",
    pricing_path: Path | str | None = None,
) -> FastAPI:
    """Create the FastAPI application backed by the specified DuckDB path."""
    storage = FocusStorage(db_path)
    pricing = PricingBook.load(pricing_path)
    app = FastAPI(
        title="Focus Ledger FinOps & Control Plane API",
        version="0.3.0",
        description="Unified FinOps FOCUS 1.5 pricing, attribution, anomaly detection, and Prometheus API.",
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        stats = storage.summary_stats()
        return {
            "status": "ok",
            "version": "0.3.0",
            "db_path": str(storage.db_path),
            **stats,
        }

    @app.get("/metrics")
    def metrics() -> Response:
        payload = build_metrics_payload(storage)
        return Response(content=payload, media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/pricing/sku")
    def sku_pricing(
        resource_type: str = Query(..., description="Terraform/OpenTofu resource type"),
        instance_type: str | None = Query(None, description="Optional SKU / instance class"),
    ) -> dict[str, Any]:
        return pricing.get_cloud_sku_rate(resource_type, instance_type)

    @app.get("/v1/anomalies")
    def anomalies(z_threshold: float = Query(3.0, ge=1.0)) -> dict[str, Any]:
        hits = detect_spend_anomalies(storage, z_threshold=z_threshold)
        return {"z_threshold": z_threshold, "count": len(hits), "anomalies": hits}

    @app.post("/v1/events/savings")
    def record_savings(event: RightsizingSavingsEvent) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        b_start, b_end = month_bounds(now)
        rec = FocusRecord(
            BillingAccountId="truplane-safescale",
            BillingAccountName="Truplane K8s SafeScale",
            BillingCurrency=pricing.currency,
            BillingPeriodStart=b_start,
            BillingPeriodEnd=b_end,
            ChargeCategory="Adjustment",
            ChargeClass="Regular",
            ChargeDescription=(
                f"Verified rightsizing savings for {event.namespace}/{event.workload} "
                f"(cpu={event.cpu_milli_saved}m, mem={event.memory_mib_saved}Mi)"
            ),
            ChargeFrequency="Recurring",
            ChargePeriodStart=now,
            ChargePeriodEnd=now,
            ConsumedQuantity=float(max(event.cpu_milli_saved, 0)),
            ConsumedUnit="CPU Millicores Reclaimed",
            ContractedCost=event.monthly_savings_usd,
            ContractedUnitPrice=event.monthly_savings_usd,
            EffectiveCost=event.monthly_savings_usd,
            InvoiceIssuerName="Truplane",
            ListCost=event.monthly_savings_usd,
            ListUnitPrice=event.monthly_savings_usd,
            PricingCategory="Adjustment",
            PricingQuantity=1.0,
            PricingUnit="Monthly Savings",
            ProviderName="Kubernetes",
            PublisherName="k8s-safescale",
            RegionId=event.namespace,
            RegionName=event.namespace,
            ResourceId=f"{event.namespace}/{event.workload}",
            ResourceName=event.workload,
            ResourceType="Kubernetes Rightsizing Savings",
            ServiceCategory="Compute",
            ServiceName="K8s SafeScale Rightsizing",
            SkuId=f"safescale:{event.policy}",
            SkuPriceId="safescale:verified-savings",
            SubAccountId=event.project_id,
            SubAccountName=f"{event.team}/{event.project_id}",
            Tags={"policy": event.policy, "namespace": event.namespace, "team": event.team},
            x_record_id=deterministic_id("k8s-safescale", event.namespace, event.policy, now.isoformat()),
            x_source="k8s-safescale",
            x_team=event.team,
            x_project_id=event.project_id,
            x_caller=event.policy,
        )
        storage.insert_records([rec], write_parquet=True)
        return {"status": "recorded", "record_id": rec.x_record_id, "monthly_savings_usd": event.monthly_savings_usd}

    @app.post("/v1/events/quality-cost")
    def record_quality_cost(event: DataQualityCostEvent) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        b_start, b_end = month_bounds(now)
        rec = FocusRecord(
            BillingAccountId="truplane-datasnap",
            BillingAccountName="Truplane Data Snap",
            BillingCurrency=pricing.currency,
            BillingPeriodStart=b_start,
            BillingPeriodEnd=b_end,
            ChargeCategory="Usage",
            ChargeClass="Regular",
            ChargeDescription=(
                f"Data quality regression reprocessing cost on {event.pipeline} "
                f"({event.failed_checks} failed checks)"
            ),
            ChargeFrequency="Usage-Based",
            ChargePeriodStart=now,
            ChargePeriodEnd=now,
            ConsumedQuantity=float(event.failed_checks),
            ConsumedUnit="Failed Regression Checks",
            ContractedCost=event.reprocessing_cost_usd,
            ContractedUnitPrice=event.reprocessing_cost_usd,
            EffectiveCost=event.reprocessing_cost_usd,
            InvoiceIssuerName="Truplane",
            ListCost=event.reprocessing_cost_usd,
            ListUnitPrice=event.reprocessing_cost_usd,
            PricingCategory="Standard",
            PricingQuantity=1.0,
            PricingUnit="Reprocessing Incident",
            ProviderName="DataPipeline",
            PublisherName="data-snap",
            RegionId="global",
            RegionName="Global",
            ResourceId=event.dataset,
            ResourceName=event.pipeline,
            ResourceType="Data Quality Incident",
            ServiceCategory="Analytics",
            ServiceName="Data Snap Cost of Quality",
            SkuId=f"datasnap:{event.pipeline}",
            SkuPriceId="datasnap:reprocessing",
            SubAccountId=event.project_id,
            SubAccountName=f"{event.team}/{event.project_id}",
            Tags={"pipeline": event.pipeline, "dataset": event.dataset, "team": event.team},
            x_record_id=deterministic_id("data-snap", event.pipeline, event.dataset, now.isoformat()),
            x_source="data-snap",
            x_team=event.team,
            x_project_id=event.project_id,
            x_caller=event.pipeline,
        )
        storage.insert_records([rec], write_parquet=True)
        return {"status": "recorded", "record_id": rec.x_record_id, "reprocessing_cost_usd": event.reprocessing_cost_usd}

    return app
