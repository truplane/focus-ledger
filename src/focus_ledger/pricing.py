"""Pricing loader and deterministic token/resource cost calculation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ModelRate:
    """Per-1M-token USD rates for a specific AI model."""

    provider: str
    input_per_1m: float
    output_per_1m: float
    cached_per_1m: float = 0.0


@dataclass(frozen=True)
class TokenCostBreakdown:
    """Computed cost components for an AI gateway inference call."""

    input_cost: float
    output_cost: float
    cached_cost: float
    total_cost: float
    blended_unit_price: float
    provider: str


DEFAULT_PRICING: dict[str, Any] = {
    "currency": "USD",
    "ai_models": {
        "gpt-4o": {
            "provider": "OpenAI",
            "input_per_1m": 2.50,
            "output_per_1m": 10.00,
            "cached_per_1m": 1.25,
        },
        "gpt-4o-mini": {
            "provider": "OpenAI",
            "input_per_1m": 0.15,
            "output_per_1m": 0.60,
            "cached_per_1m": 0.075,
        },
        "claude-3-5-sonnet": {
            "provider": "Anthropic",
            "input_per_1m": 3.00,
            "output_per_1m": 15.00,
            "cached_per_1m": 0.30,
        },
        "claude-3-5-haiku": {
            "provider": "Anthropic",
            "input_per_1m": 0.80,
            "output_per_1m": 4.00,
            "cached_per_1m": 0.08,
        },
        "llama-3.3-70b-instruct": {
            "provider": "LiteLLM",
            "input_per_1m": 0.59,
            "output_per_1m": 0.79,
            "cached_per_1m": 0.30,
        },
        "mirevox/smart-router-auto": {
            "provider": "Mirevox",
            "input_per_1m": 0.90,
            "output_per_1m": 2.70,
            "cached_per_1m": 0.25,
        },
        "default": {
            "provider": "AI Gateway",
            "input_per_1m": 1.00,
            "output_per_1m": 3.00,
            "cached_per_1m": 0.25,
        },
    },
    "kubernetes": {
        "cpu_core_hour_usd": 0.031611,
        "ram_gb_hour_usd": 0.004237,
        "pv_gb_hour_usd": 0.00014,
    },
    "warehouse": {
        "default_credit_usd": 3.00,
        "platforms": {
            "snowflake": {"credit_usd": 3.00},
            "databricks": {"credit_usd": 0.55},
        },
        "cost_centers": {},
    },
}


class PricingBook:
    """Loads pricing.yaml and resolves AI model, Kubernetes, and warehouse rates."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.currency: str = str(raw.get("currency", "USD"))
        self.ai_models: dict[str, ModelRate] = {}
        for name, spec in raw.get("ai_models", {}).items():
            self.ai_models[name.lower()] = ModelRate(
                provider=str(spec.get("provider", "AI Gateway")),
                input_per_1m=float(spec.get("input_per_1m", 1.0)),
                output_per_1m=float(spec.get("output_per_1m", 3.0)),
                cached_per_1m=float(spec.get("cached_per_1m", 0.25)),
            )
        self.kubernetes: dict[str, float] = {
            k: float(v) for k, v in raw.get("kubernetes", {}).items()
        }
        self.warehouse: dict[str, Any] = dict(raw.get("warehouse", {}))

    @classmethod
    def load(cls, path: Path | str | None = None) -> "PricingBook":
        """Load pricing configuration from path, falling back to repo or built-in defaults."""
        candidate_paths = []
        if path is not None:
            candidate_paths.append(Path(path))
        candidate_paths.append(Path.cwd() / "pricing.yaml")
        candidate_paths.append(Path(__file__).resolve().parents[2] / "pricing.yaml")

        for candidate in candidate_paths:
            if candidate.is_file():
                with candidate.open("r", encoding="utf-8") as fh:
                    loaded = yaml.safe_load(fh) or {}
                merged = {**DEFAULT_PRICING, **loaded}
                return cls(merged)
        return cls(DEFAULT_PRICING)

    def get_model_rate(self, model: str) -> ModelRate:
        """Resolve token pricing for a model name (supports provider prefixes)."""
        normalized = model.strip().lower()
        if normalized in self.ai_models:
            return self.ai_models[normalized]
        # Match stripped provider prefix like "openai/gpt-4o" -> "gpt-4o"
        if "/" in normalized:
            short_name = normalized.split("/", 1)[1]
            if short_name in self.ai_models:
                return self.ai_models[short_name]
        return self.ai_models.get(
            "default",
            ModelRate(provider="AI Gateway", input_per_1m=1.0, output_per_1m=3.0, cached_per_1m=0.25),
        )

    def calculate_token_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
    ) -> TokenCostBreakdown:
        """Compute exact USD cost for input, output, and cached prompt tokens."""
        rate = self.get_model_rate(model)
        billable_input = max(0, input_tokens - cached_tokens)
        input_cost = (billable_input / 1_000_000.0) * rate.input_per_1m
        output_cost = (max(0, output_tokens) / 1_000_000.0) * rate.output_per_1m
        cached_cost = (max(0, cached_tokens) / 1_000_000.0) * rate.cached_per_1m
        total_cost = round(input_cost + output_cost + cached_cost, 10)
        total_tokens = max(0, input_tokens) + max(0, output_tokens)
        blended = (total_cost / total_tokens) if total_tokens > 0 else 0.0
        return TokenCostBreakdown(
            input_cost=round(input_cost, 10),
            output_cost=round(output_cost, 10),
            cached_cost=round(cached_cost, 10),
            total_cost=total_cost,
            blended_unit_price=round(blended, 12),
            provider=rate.provider,
        )

    def get_warehouse_credit_usd(self, platform: str) -> float:
        """Return USD rate per credit/DBU for the given warehouse platform."""
        platforms = self.warehouse.get("platforms", {})
        entry = platforms.get(platform.strip().lower(), {})
        if "credit_usd" in entry:
            return float(entry["credit_usd"])
        return float(self.warehouse.get("default_credit_usd", 3.0))

    def get_warehouse_cost_center(self, warehouse_name: str) -> tuple[str, str] | None:
        """Look up (team, project_id) mapped to a warehouse name in pricing.yaml."""
        centers = self.warehouse.get("cost_centers", {})
        if warehouse_name in centers:
            cfg = centers[warehouse_name]
            return str(cfg.get("team", "unallocated")), str(cfg.get("project_id", "default"))
        return None
