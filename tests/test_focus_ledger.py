"""Unit and integration tests for Focus Ledger."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from focus_ledger.cli import app as cli_app
from focus_ledger.ingestors import get_ingestor
from focus_ledger.pricing import PricingBook
from focus_ledger.schema import FOCUS_COLUMNS, FocusRecord
from focus_ledger.server import create_app
from focus_ledger.storage import FocusStorage, GroupByDimension

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
PRICING_FILE = Path(__file__).resolve().parents[1] / "pricing.yaml"


def test_token_cost_math_with_cached_tokens() -> None:
    pricing = PricingBook.load(PRICING_FILE)
    # gpt-4o: input=$2.50/1M, output=$10.00/1M, cached=$1.25/1M
    # input=12000 (4000 cached -> 8000 uncached), output=3500
    breakdown = pricing.calculate_token_cost(
        model="gpt-4o",
        input_tokens=12_000,
        output_tokens=3_500,
        cached_tokens=4_000,
    )
    expected_input = (8_000 / 1_000_000) * 2.50  # 0.02
    expected_output = (3_500 / 1_000_000) * 10.00  # 0.035
    expected_cached = (4_000 / 1_000_000) * 1.25  # 0.005
    expected_total = round(expected_input + expected_output + expected_cached, 10)  # 0.06
    assert breakdown.input_cost == pytest.approx(expected_input)
    assert breakdown.output_cost == pytest.approx(expected_output)
    assert breakdown.cached_cost == pytest.approx(expected_cached)
    assert breakdown.total_cost == pytest.approx(expected_total)
    assert breakdown.provider == "OpenAI"


def test_ai_gateway_normalization_and_schema() -> None:
    pricing = PricingBook.load(PRICING_FILE)
    ingestor = get_ingestor("ai-gateway")
    records = ingestor.parse(EXAMPLES_DIR / "ai_gateway_sample.jsonl", pricing)
    assert len(records) == 5

    first = records[0]
    assert isinstance(first, FocusRecord)
    assert first.ServiceCategory == "AI and Machine Learning"
    assert first.ConsumedUnit == "Tokens"
    assert first.x_team == "ml-platform"
    assert first.x_project_id == "agent-copilot"
    assert first.x_agent_id == "planner-v2"
    assert first.EffectiveCost == pytest.approx(0.06)

    # Verify Anthropic nested usage format normalization
    second = records[1]
    assert second.x_model == "claude-3-5-sonnet"
    assert second.x_input_tokens == 25_000
    assert second.x_output_tokens == 6_000
    assert second.x_cached_tokens == 10_000
    # (15k * 3.00 + 6k * 15.00 + 10k * 0.30) / 1M = 0.045 + 0.090 + 0.003 = 0.138
    assert second.EffectiveCost == pytest.approx(0.138)


def test_k8s_and_warehouse_ingestors() -> None:
    pricing = PricingBook.load(PRICING_FILE)

    k8s_records = get_ingestor("k8s-opencost").parse(
        EXAMPLES_DIR / "k8s_opencost_sample.json", pricing
    )
    assert len(k8s_records) == 3
    assert k8s_records[0].ServiceName == "Kubernetes Compute"
    assert k8s_records[0].x_team == "ml-platform"
    assert k8s_records[0].EffectiveCost == pytest.approx(9.7573)

    wh_records = get_ingestor("warehouse").parse(
        EXAMPLES_DIR / "warehouse_sample.csv", pricing
    )
    assert len(wh_records) == 4
    # Row 3 omits team/project_id in CSV so pricing.yaml cost_center mapping applies
    etl_row = wh_records[2]
    assert etl_row.x_team == "data-engineering"
    assert etl_row.x_project_id == "core-etl"
    assert etl_row.EffectiveCost == pytest.approx(3.80 * 3.00)

    dbx_row = wh_records[3]
    assert dbx_row.ProviderName == "Databricks"
    assert dbx_row.ConsumedUnit == "DBUs"
    assert dbx_row.EffectiveCost == pytest.approx(4.50 * 0.55)


def test_duckdb_storage_partitioned_parquet_and_aggregation(tmp_path: Path) -> None:
    pricing = PricingBook.load(PRICING_FILE)
    db_path = tmp_path / "test_ledger.duckdb"
    storage = FocusStorage(db_path)

    all_records = []
    all_records.extend(
        get_ingestor("ai-gateway").parse(EXAMPLES_DIR / "ai_gateway_sample.jsonl", pricing)
    )
    all_records.extend(
        get_ingestor("k8s-opencost").parse(EXAMPLES_DIR / "k8s_opencost_sample.json", pricing)
    )
    all_records.extend(
        get_ingestor("warehouse").parse(EXAMPLES_DIR / "warehouse_sample.csv", pricing)
    )

    inserted = storage.insert_records(all_records, write_parquet=True)
    assert inserted == 12

    # Check Hive-partitioned Parquet directories exist for each source and billing_month
    for source in ("ai-gateway", "k8s-opencost", "warehouse"):
        part_dir = storage.parquet_dir / f"x_source={source}"
        assert part_dir.is_dir()
        assert any(part_dir.glob("billing_month=*/data_0.parquet"))

    # Verify Parquet file contains FOCUS 1.5 columns
    con = duckdb.connect()
    cols = [
        r[0]
        for r in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{(storage.parquet_dir / '**' / '*.parquet').as_posix()}', hive_partitioning=false)"
        ).fetchall()
    ]
    for required_col in ("BillingAccountId", "EffectiveCost", "ServiceName", "x_team", "x_model"):
        assert required_col in cols
    assert len(cols) == len(FOCUS_COLUMNS)

    by_team = storage.aggregate(GroupByDimension.team)
    teams = {row["dimension"]: row for row in by_team}
    assert "ml-platform" in teams
    assert "data-engineering" in teams
    assert teams["ml-platform"]["effective_cost_usd"] > 0

    parquet_report = tmp_path / "team_report.parquet"
    storage.export_report_parquet(GroupByDimension.team, parquet_report)
    assert parquet_report.is_file()


def test_cli_and_fastapi_metrics_endpoints(tmp_path: Path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "cli_ledger.duckdb"

    for src, fname in (
        ("ai-gateway", "ai_gateway_sample.jsonl"),
        ("k8s-opencost", "k8s_opencost_sample.json"),
        ("warehouse", "warehouse_sample.csv"),
    ):
        res = runner.invoke(
            cli_app,
            [
                "ingest",
                "--source",
                src,
                "--input",
                str(EXAMPLES_DIR / fname),
                "--db",
                str(db_path),
                "--pricing",
                str(PRICING_FILE),
            ],
        )
        assert res.exit_code == 0, res.output

    report_res = runner.invoke(
        cli_app,
        ["report", "--db", str(db_path), "--group-by", "model", "--format", "json"],
    )
    assert report_res.exit_code == 0, report_res.output
    payload = json.loads(report_res.stdout)
    assert payload["group_by"] == "model"
    models = {g["dimension"] for g in payload["groups"]}
    assert "gpt-4o" in models
    assert "claude-3-5-sonnet" in models

    # FastAPI /health and /metrics
    api = create_app(db_path)
    client = TestClient(api)
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    health_json = health_resp.json()
    assert health_json["status"] == "ok"
    assert health_json["record_count"] == 12
    assert health_json["total_effective_cost_usd"] > 40.0

    metrics_resp = client.get("/metrics")
    assert metrics_resp.status_code == 200
    text = metrics_resp.text
    assert "focus_ledger_effective_cost_usd{" in text
    assert 'team="ml-platform"' in text
    assert 'model="gpt-4o"' in text
    assert "focus_ledger_total_effective_cost_usd" in text


def test_incremental_partition_refresh_preserves_untouched_months(tmp_path: Path) -> None:
    pricing = PricingBook.load(PRICING_FILE)
    ingestor = get_ingestor("ai-gateway")

    nov_jsonl = tmp_path / "nov.jsonl"
    dec_jsonl = tmp_path / "dec.jsonl"
    nov_jsonl.write_text(
        json.dumps(
            {
                "request_id": "req-nov-1",
                "timestamp": "2026-11-15T10:00:00Z",
                "model": "gpt-4o",
                "input_tokens": 1000,
                "output_tokens": 500,
                "team": "ml-platform",
                "project_id": "copilot",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    dec_jsonl.write_text(
        json.dumps(
            {
                "request_id": "req-dec-1",
                "timestamp": "2026-12-01T10:00:00Z",
                "model": "gpt-4o-mini",
                "input_tokens": 2000,
                "output_tokens": 500,
                "team": "ml-platform",
                "project_id": "copilot",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    storage = FocusStorage(tmp_path / "inc.duckdb")
    storage.insert_records(ingestor.parse(nov_jsonl, pricing), write_parquet=True)
    nov_pq = (
        storage.parquet_dir
        / "x_source=ai-gateway"
        / "billing_month=2026-11"
        / "data_0.parquet"
    )
    assert nov_pq.is_file()
    nov_mtime_before = nov_pq.stat().st_mtime_ns

    storage.insert_records(ingestor.parse(dec_jsonl, pricing), write_parquet=True)
    dec_pq = (
        storage.parquet_dir
        / "x_source=ai-gateway"
        / "billing_month=2026-12"
        / "data_0.parquet"
    )
    assert dec_pq.is_file()
    assert nov_pq.stat().st_mtime_ns == nov_mtime_before

