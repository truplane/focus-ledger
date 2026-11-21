"""Typer CLI entrypoint for Focus Ledger."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Optional

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from focus_ledger.ingestors import get_ingestor, list_sources
from focus_ledger.pricing import PricingBook
from focus_ledger.server import create_app
from focus_ledger.storage import FocusStorage, GroupByDimension

app = typer.Typer(
    name="focus-ledger",
    help="Unified FinOps & AI Cost Attribution Engine normalizing spend to FOCUS 1.5.",
    no_args_is_help=True,
)
console = Console()


class ReportFormat(str, Enum):
    """Supported output formats for cost reports."""

    table = "table"
    json = "json"
    parquet = "parquet"


@app.command()
def ingest(
    source: str = typer.Option(
        ...,
        "--source",
        "-s",
        help="Telemetry source: ai-gateway, k8s-opencost, or warehouse.",
    ),
    input_file: Path = typer.Option(
        ...,
        "--input",
        "-i",
        exists=True,
        readable=True,
        help="Path to raw telemetry JSONL, JSON, or CSV file.",
    ),
    db: Path = typer.Option(
        Path("focus_ledger.duckdb"),
        "--db",
        help="Path to embedded DuckDB database file.",
    ),
    pricing_file: Optional[Path] = typer.Option(
        None,
        "--pricing",
        help="Optional path to pricing.yaml price sheet.",
    ),
) -> None:
    """Ingest telemetry into FOCUS 1.5 DuckDB storage and partitioned Parquet."""
    try:
        ingestor = get_ingestor(source)
    except KeyError as exc:
        typer.secho(
            f"{exc}. Available: {', '.join(list_sources())}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from exc

    pricing = PricingBook.load(pricing_file)
    records = ingestor.parse(input_file, pricing)
    storage = FocusStorage(db)
    count = storage.insert_records(records, write_parquet=True)
    total_cost = round(sum(r.EffectiveCost for r in records), 6)
    typer.echo(
        f"Ingested {count} FOCUS 1.5 records from '{source}' "
        f"(total EffectiveCost=${total_cost:.6f}) -> {db}"
    )


@app.command()
def report(
    db: Path = typer.Option(
        Path("focus_ledger.duckdb"),
        "--db",
        help="Path to embedded DuckDB database file.",
    ),
    group_by: GroupByDimension = typer.Option(
        GroupByDimension.team,
        "--group-by",
        "-g",
        help="Aggregation dimension: team, project, model, or service.",
    ),
    output_format: ReportFormat = typer.Option(
        ReportFormat.table,
        "--format",
        "-f",
        help="Output format: table, json, or parquet.",
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        "-o",
        help="Output path when using --format parquet (or optional file output).",
    ),
) -> None:
    """Generate a FOCUS 1.5 cost attribution report grouped by team, project, model, or service."""
    storage = FocusStorage(db)
    rows = storage.aggregate(group_by)

    if output_format == ReportFormat.parquet:
        target = out or Path(f"focus_report_{group_by.value}.parquet")
        storage.export_report_parquet(group_by, target)
        typer.echo(f"Wrote Parquet report ({len(rows)} groups) -> {target}")
        return

    if output_format == ReportFormat.json:
        payload = json.dumps(
            {"group_by": group_by.value, "groups": rows},
            indent=2,
        )
        if out is not None:
            out.write_text(payload, encoding="utf-8")
            typer.echo(f"Wrote JSON report -> {out}")
        else:
            typer.echo(payload)
        return

    table = Table(title=f"FOCUS 1.5 Cost Attribution by {group_by.value}")
    table.add_column(group_by.value.capitalize(), style="cyan", no_wrap=True)
    table.add_column("Records", justify="right")
    table.add_column("Effective Cost (USD)", justify="right", style="green")
    table.add_column("List Cost (USD)", justify="right")
    table.add_column("Total Tokens", justify="right")
    table.add_column("Consumed Qty", justify="right")

    for row in rows:
        table.add_row(
            str(row["dimension"]),
            str(row["record_count"]),
            f"${float(row['effective_cost_usd']):.6f}",
            f"${float(row['list_cost_usd']):.6f}",
            f"{int(row['total_tokens']):,}",
            f"{float(row['consumed_quantity']):,.4f}",
        )
    console.print(table)


@app.command()
def serve(
    port: int = typer.Option(9110, "--port", "-p", help="HTTP port for /metrics and /health."),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host address."),
    db: Path = typer.Option(
        Path("focus_ledger.duckdb"),
        "--db",
        help="Path to embedded DuckDB database file.",
    ),
) -> None:
    """Start the Prometheus metrics and health HTTP server."""
    fastapi_app = create_app(db)
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
