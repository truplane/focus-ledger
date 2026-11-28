"""Statistical 3-sigma spend anomaly detection over DuckDB FOCUS 1.5 records."""

from __future__ import annotations

import math
from typing import Any

from focus_ledger.storage import FocusStorage


def detect_spend_anomalies(
    storage: FocusStorage,
    z_threshold: float = 3.0,
) -> list[dict[str, Any]]:
    """Detect spend spikes exceeding `z_threshold` standard deviations per (source, team).

    Uses leave-one-out baseline statistics on the other samples in each
    (x_source, x_team) series so an extreme outlier does not mask itself.
    """
    if not storage.db_path.exists():
        return []

    with storage._connect(read_only=True) as con:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if "focus_ledger" not in tables:
            return []
        rows = con.execute(
            """
            SELECT x_source, x_team, x_project_id, ResourceId, EffectiveCost,
                   CAST(ChargePeriodStart AS VARCHAR)
            FROM focus_ledger
            WHERE EffectiveCost > 0
              AND ChargeCategory = 'Usage'
            ORDER BY ChargePeriodStart ASC, x_record_id ASC
            """
        ).fetchall()

    grouped: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for row in rows:
        key = (str(row[0]), str(row[1]))
        grouped.setdefault(key, []).append(row)

    anomalies: list[dict[str, Any]] = []
    for (source, team), series in grouped.items():
        if len(series) < 4:
            continue
        values = [float(r[4]) for r in series]
        for idx, row in enumerate(series):
            val = values[idx]
            baseline_vals = values[:idx] + values[idx + 1 :]
            mean = sum(baseline_vals) / len(baseline_vals)
            variance = sum((v - mean) ** 2 for v in baseline_vals) / len(baseline_vals)
            std = math.sqrt(variance)
            if std <= 1e-12:
                if val > mean * 3.0 and (val - mean) > 0.01:
                    z_score = 99.0
                else:
                    continue
            else:
                z_score = (val - mean) / std
            if z_score >= z_threshold:
                anomalies.append(
                    {
                        "source": source,
                        "team": team,
                        "project_id": str(row[2]),
                        "resource_id": str(row[3]),
                        "effective_cost_usd": round(val, 6),
                        "baseline_mean_usd": round(mean, 6),
                        "baseline_stddev_usd": round(std, 6),
                        "z_score": round(z_score, 3),
                        "threshold": z_threshold,
                        "timestamp": str(row[5]),
                    }
                )
    anomalies.sort(key=lambda a: a["z_score"], reverse=True)
    return anomalies
