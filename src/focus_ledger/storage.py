"""Embedded DuckDB storage and partitioned FOCUS 1.5 Parquet writer."""

from __future__ import annotations

import shutil
from enum import Enum
from pathlib import Path
from typing import Any

import duckdb

from focus_ledger.schema import FOCUS_COLUMNS, FocusRecord


class GroupByDimension(str, Enum):
    """Supported cost aggregation dimensions."""

    team = "team"
    project = "project"
    model = "model"
    service = "service"


_GROUP_COLUMN_MAP: dict[GroupByDimension, str] = {
    GroupByDimension.team: "x_team",
    GroupByDimension.project: "x_project_id",
    GroupByDimension.model: "x_model",
    GroupByDimension.service: "ServiceName",
}

_DDL = """
CREATE TABLE IF NOT EXISTS focus_ledger (
    BillingAccountId VARCHAR,
    BillingAccountName VARCHAR,
    BillingCurrency VARCHAR,
    BillingPeriodStart TIMESTAMPTZ,
    BillingPeriodEnd TIMESTAMPTZ,
    ChargeCategory VARCHAR,
    ChargeClass VARCHAR,
    ChargeDescription VARCHAR,
    ChargeFrequency VARCHAR,
    ChargePeriodStart TIMESTAMPTZ,
    ChargePeriodEnd TIMESTAMPTZ,
    CommitmentDiscountCategory VARCHAR,
    CommitmentDiscountId VARCHAR,
    CommitmentDiscountName VARCHAR,
    CommitmentDiscountStatus VARCHAR,
    CommitmentDiscountType VARCHAR,
    ConsumedQuantity DOUBLE,
    ConsumedUnit VARCHAR,
    ContractedCost DOUBLE,
    ContractedUnitPrice DOUBLE,
    EffectiveCost DOUBLE,
    InvoiceIssuerName VARCHAR,
    ListCost DOUBLE,
    ListUnitPrice DOUBLE,
    PricingCategory VARCHAR,
    PricingQuantity DOUBLE,
    PricingUnit VARCHAR,
    ProviderName VARCHAR,
    PublisherName VARCHAR,
    RegionId VARCHAR,
    RegionName VARCHAR,
    ResourceId VARCHAR,
    ResourceName VARCHAR,
    ResourceType VARCHAR,
    ServiceCategory VARCHAR,
    ServiceName VARCHAR,
    SkuId VARCHAR,
    SkuPriceId VARCHAR,
    SubAccountId VARCHAR,
    SubAccountName VARCHAR,
    Tags VARCHAR,
    x_record_id VARCHAR PRIMARY KEY,
    x_source VARCHAR,
    x_team VARCHAR,
    x_project_id VARCHAR,
    x_model VARCHAR,
    x_caller VARCHAR,
    x_agent_id VARCHAR,
    x_input_tokens BIGINT,
    x_output_tokens BIGINT,
    x_cached_tokens BIGINT,
    x_total_tokens BIGINT
);
"""


class FocusStorage:
    """Manages embedded DuckDB table and Hive-partitioned Parquet output."""

    def __init__(self, db_path: Path | str, parquet_dir: Path | str | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.parquet_dir = (
            Path(parquet_dir)
            if parquet_dir is not None
            else self.db_path.parent / f"{self.db_path.stem}_parquet"
        )

    def _connect(self, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.db_path), read_only=read_only)

    def ensure_schema(self, con: duckdb.DuckDBPyConnection) -> None:
        """Create the canonical FOCUS 1.5 DuckDB table if absent."""
        con.execute(_DDL)

    def insert_records(self, records: list[FocusRecord], write_parquet: bool = True) -> int:
        """Upsert normalized FocusRecords into DuckDB and refresh partitioned Parquet."""
        if not records:
            with self._connect() as con:
                self.ensure_schema(con)
            return 0

        placeholders = ", ".join(["?"] * len(FOCUS_COLUMNS))
        column_list = ", ".join(FOCUS_COLUMNS)
        sql = f"INSERT OR REPLACE INTO focus_ledger ({column_list}) VALUES ({placeholders})"

        rows = [
            [row_dict[col] for col in FOCUS_COLUMNS]
            for row_dict in (rec.to_storage_row() for rec in records)
        ]

        with self._connect() as con:
            self.ensure_schema(con)
            con.executemany(sql, rows)
            if write_parquet:
                self._export_partitioned_parquet(con)
        return len(records)

    def _export_partitioned_parquet(self, con: duckdb.DuckDBPyConnection) -> None:
        """Write full FOCUS 1.5 ledger partitioned by x_source as Parquet."""
        staging = self.parquet_dir.parent / f".{self.parquet_dir.name}.tmp"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        posix_target = staging.as_posix().replace("'", "''")
        con.execute(
            f"COPY (SELECT * FROM focus_ledger) TO '{posix_target}' "
            "(FORMAT PARQUET, PARTITION_BY (x_source))"
        )
        if self.parquet_dir.exists():
            shutil.rmtree(self.parquet_dir)
        staging.rename(self.parquet_dir)

    def aggregate(self, group_by: GroupByDimension) -> list[dict[str, Any]]:
        """Aggregate FOCUS spend and token usage by the requested dimension."""
        if not self.db_path.exists():
            return []
        col = _GROUP_COLUMN_MAP[group_by]
        query = f"""
            SELECT
                COALESCE({col}, 'unknown') AS dimension,
                COUNT(*) AS record_count,
                ROUND(SUM(EffectiveCost), 6) AS effective_cost_usd,
                ROUND(SUM(ListCost), 6) AS list_cost_usd,
                SUM(x_total_tokens) AS total_tokens,
                ROUND(SUM(ConsumedQuantity), 4) AS consumed_quantity
            FROM focus_ledger
            GROUP BY 1
            ORDER BY effective_cost_usd DESC, dimension ASC
        """
        with self._connect(read_only=True) as con:
            tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
            if "focus_ledger" not in tables:
                return []
            cursor = con.execute(query)
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def export_report_parquet(self, group_by: GroupByDimension, out_path: Path | str) -> Path:
        """Export aggregated cost report directly to a Parquet file via DuckDB."""
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        col = _GROUP_COLUMN_MAP[group_by]
        posix_out = target.as_posix().replace("'", "''")
        with self._connect(read_only=True) as con:
            con.execute(
                f"""
                COPY (
                    SELECT
                        COALESCE({col}, 'unknown') AS {group_by.value},
                        COUNT(*) AS record_count,
                        ROUND(SUM(EffectiveCost), 6) AS effective_cost_usd,
                        ROUND(SUM(ListCost), 6) AS list_cost_usd,
                        SUM(x_total_tokens) AS total_tokens,
                        ROUND(SUM(ConsumedQuantity), 4) AS consumed_quantity
                    FROM focus_ledger
                    GROUP BY 1
                    ORDER BY effective_cost_usd DESC
                ) TO '{posix_out}' (FORMAT PARQUET)
                """
            )
        return target

    def summary_stats(self) -> dict[str, Any]:
        """Return record counts and total effective cost across the ledger."""
        if not self.db_path.exists():
            return {"record_count": 0, "total_effective_cost_usd": 0.0, "sources": {}}
        with self._connect(read_only=True) as con:
            tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
            if "focus_ledger" not in tables:
                return {"record_count": 0, "total_effective_cost_usd": 0.0, "sources": {}}
            total_row = con.execute(
                "SELECT COUNT(*), COALESCE(ROUND(SUM(EffectiveCost), 6), 0.0) FROM focus_ledger"
            ).fetchone()
            source_rows = con.execute(
                "SELECT x_source, COUNT(*) FROM focus_ledger GROUP BY x_source"
            ).fetchall()
        return {
            "record_count": int(total_row[0]),
            "total_effective_cost_usd": float(total_row[1]),
            "sources": {str(src): int(cnt) for src, cnt in source_rows},
        }

    def metrics_breakdown(self) -> dict[str, list[tuple[Any, ...]]]:
        """Query dimensional breakdowns for Prometheus metric exposition."""
        empty = {"cost": [], "quantity": [], "tokens": [], "records": []}
        if not self.db_path.exists():
            return empty
        with self._connect(read_only=True) as con:
            tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
            if "focus_ledger" not in tables:
                return empty
            cost_rows = con.execute(
                """
                SELECT x_team, x_project_id, x_model, x_source, ServiceName, SUM(EffectiveCost)
                FROM focus_ledger
                GROUP BY 1, 2, 3, 4, 5
                """
            ).fetchall()
            qty_rows = con.execute(
                """
                SELECT x_team, x_project_id, x_model, x_source, ConsumedUnit, SUM(ConsumedQuantity)
                FROM focus_ledger
                GROUP BY 1, 2, 3, 4, 5
                """
            ).fetchall()
            token_rows = con.execute(
                """
                SELECT x_team, x_project_id, x_model,
                       SUM(x_input_tokens), SUM(x_output_tokens), SUM(x_cached_tokens)
                FROM focus_ledger
                WHERE x_source = 'ai-gateway'
                GROUP BY 1, 2, 3
                """
            ).fetchall()
            record_rows = con.execute(
                "SELECT x_source, COUNT(*) FROM focus_ledger GROUP BY 1"
            ).fetchall()
        return {
            "cost": cost_rows,
            "quantity": qty_rows,
            "tokens": token_rows,
            "records": record_rows,
        }
