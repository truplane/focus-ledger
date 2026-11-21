"""AI Gateway JSONL ingestor (OpenAI, Anthropic, LiteLLM, Mirevox Smart Router)."""

from __future__ import annotations

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


def _extract_tokens(entry: dict[str, Any]) -> tuple[int, int, int]:
    """Normalize token fields across OpenAI, Anthropic, LiteLLM, and Mirevox log shapes."""
    usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
    prompt_details = (
        usage.get("prompt_tokens_details")
        if isinstance(usage.get("prompt_tokens_details"), dict)
        else {}
    )
    input_tokens = int(
        entry.get("input_tokens")
        or usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or entry.get("prompt_tokens")
        or 0
    )
    output_tokens = int(
        entry.get("output_tokens")
        or usage.get("output_tokens")
        or usage.get("completion_tokens")
        or entry.get("completion_tokens")
        or 0
    )
    cached_tokens = int(
        entry.get("cached_tokens")
        or usage.get("cache_read_input_tokens")
        or prompt_details.get("cached_tokens")
        or 0
    )
    return input_tokens, output_tokens, cached_tokens


@register_ingestor
class AIGatewayIngestor(BaseIngestor):
    """Normalizes AI inference gateway JSONL telemetry into FOCUS 1.5 records."""

    source_name = "ai-gateway"

    def parse(self, input_path: Path, pricing: PricingBook) -> list[FocusRecord]:
        records: list[FocusRecord] = []
        with input_path.open("r", encoding="utf-8") as fh:
            for line_num, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                model = str(entry.get("model") or "unknown")
                input_tokens, output_tokens, cached_tokens = _extract_tokens(entry)
                total_tokens = input_tokens + output_tokens

                ts = parse_timestamp(entry.get("timestamp") or entry.get("created_at"))
                bill_start, bill_end = month_bounds(ts)
                charge_end = ts + timedelta(seconds=1)

                cost_info = pricing.calculate_token_cost(
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                )
                # Respect upstream pre-computed cost only when explicitly provided and positive
                explicit_cost = entry.get("cost_usd")
                effective_cost = (
                    float(explicit_cost)
                    if explicit_cost is not None
                    else cost_info.total_cost
                )
                blended = (effective_cost / total_tokens) if total_tokens > 0 else 0.0

                metadata = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
                team = str(entry.get("team") or metadata.get("team") or "unallocated")
                project_id = str(
                    entry.get("project_id") or metadata.get("project_id") or "default"
                )
                caller = str(
                    entry.get("caller")
                    or entry.get("user")
                    or metadata.get("caller")
                    or "unknown"
                )
                agent_id = str(
                    entry.get("agent_id")
                    or metadata.get("agent_id")
                    or caller
                )
                request_id = str(entry.get("request_id") or entry.get("id") or f"line-{line_num}")
                gateway = str(entry.get("gateway") or "ai-gateway")
                provider = str(entry.get("provider") or cost_info.provider)
                region = str(entry.get("region") or "us-east-1")

                tags = {
                    "team": team,
                    "project_id": project_id,
                    "model": model,
                    "caller": caller,
                    "agent_id": agent_id,
                    "gateway": gateway,
                }
                if isinstance(entry.get("tags"), dict):
                    for k, v in entry["tags"].items():
                        tags[str(k)] = str(v)

                record = FocusRecord(
                    BillingAccountId=f"acct-{gateway}",
                    BillingAccountName=f"{gateway} Unified Billing",
                    BillingCurrency=pricing.currency,
                    BillingPeriodStart=bill_start,
                    BillingPeriodEnd=bill_end,
                    ChargeCategory="Usage",
                    ChargeClass="Regular",
                    ChargeDescription=(
                        f"{provider} {model} inference "
                        f"(in={input_tokens}, out={output_tokens}, cached={cached_tokens})"
                    ),
                    ChargeFrequency="Usage-Based",
                    ChargePeriodStart=ts,
                    ChargePeriodEnd=charge_end,
                    ConsumedQuantity=float(total_tokens),
                    ConsumedUnit="Tokens",
                    ContractedCost=effective_cost,
                    ContractedUnitPrice=blended,
                    EffectiveCost=effective_cost,
                    InvoiceIssuerName=provider,
                    ListCost=cost_info.total_cost,
                    ListUnitPrice=cost_info.blended_unit_price,
                    PricingCategory="Standard",
                    PricingQuantity=float(total_tokens) / 1_000_000.0,
                    PricingUnit="1M Tokens",
                    ProviderName=provider,
                    PublisherName=provider,
                    RegionId=region,
                    RegionName=region,
                    ResourceId=request_id,
                    ResourceName=f"{model}/{agent_id}",
                    ResourceType="AI Inference Request",
                    ServiceCategory="AI and Machine Learning",
                    ServiceName=f"AI Gateway ({gateway})",
                    SkuId=f"{provider.lower()}:{model}",
                    SkuPriceId=f"{model}:blended-token",
                    SubAccountId=project_id,
                    SubAccountName=f"{team}/{project_id}",
                    Tags=tags,
                    x_record_id=deterministic_id(self.source_name, request_id, str(line_num)),
                    x_source=self.source_name,
                    x_team=team,
                    x_project_id=project_id,
                    x_model=model,
                    x_caller=caller,
                    x_agent_id=agent_id,
                    x_input_tokens=input_tokens,
                    x_output_tokens=output_tokens,
                    x_cached_tokens=cached_tokens,
                    x_total_tokens=total_tokens,
                )
                records.append(record)
        return records
