"""FastAPI server exposing FOCUS 1.5 health and Prometheus cost metrics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest

from focus_ledger.storage import FocusStorage


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

    breakdown = storage.metrics_breakdown()
    grand_total = 0.0
    for team, project, model, source, service, cost in breakdown["cost"]:
        val = float(cost or 0.0)
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

    total_cost_gauge.set(round(grand_total, 8))
    return generate_latest(registry)


def create_app(db_path: Path | str = "focus_ledger.duckdb") -> FastAPI:
    """Create the FastAPI application backed by the specified DuckDB path."""
    storage = FocusStorage(db_path)
    app = FastAPI(
        title="Focus Ledger FinOps Metrics API",
        version="0.1.0",
        description="Exposes FinOps FOCUS 1.5 cost attribution metrics for Prometheus.",
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        stats = storage.summary_stats()
        return {
            "status": "ok",
            "db_path": str(storage.db_path),
            **stats,
        }

    @app.get("/metrics")
    def metrics() -> Response:
        payload = build_metrics_payload(storage)
        return Response(content=payload, media_type=CONTENT_TYPE_LATEST)

    return app
