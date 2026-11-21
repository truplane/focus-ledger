"""FinOps FOCUS 1.5 canonical schema and AI/FinOps attribution extensions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class FocusRecord(BaseModel):
    """Canonical FinOps FOCUS 1.5 billing record with AI attribution extensions."""

    # Core FOCUS 1.5 billing & period metadata
    BillingAccountId: str
    BillingAccountName: str
    BillingCurrency: str = "USD"
    BillingPeriodStart: datetime
    BillingPeriodEnd: datetime

    # Charge classification
    ChargeCategory: str = "Usage"
    ChargeClass: str = "Regular"
    ChargeDescription: str
    ChargeFrequency: str = "Usage-Based"
    ChargePeriodStart: datetime
    ChargePeriodEnd: datetime

    # Commitment discount attributes
    CommitmentDiscountCategory: str | None = None
    CommitmentDiscountId: str | None = None
    CommitmentDiscountName: str | None = None
    CommitmentDiscountStatus: str | None = None
    CommitmentDiscountType: str | None = None

    # Quantities & pricing
    ConsumedQuantity: float = Field(ge=0.0)
    ConsumedUnit: str
    ContractedCost: float = Field(ge=0.0)
    ContractedUnitPrice: float = Field(ge=0.0)
    EffectiveCost: float = Field(ge=0.0)
    InvoiceIssuerName: str
    ListCost: float = Field(ge=0.0)
    ListUnitPrice: float = Field(ge=0.0)
    PricingCategory: str = "Standard"
    PricingQuantity: float = Field(ge=0.0)
    PricingUnit: str

    # Provider & service dimensions
    ProviderName: str
    PublisherName: str
    RegionId: str = "global"
    RegionName: str = "Global"
    ResourceId: str
    ResourceName: str
    ResourceType: str
    ServiceCategory: str
    ServiceName: str
    SkuId: str
    SkuPriceId: str
    SubAccountId: str
    SubAccountName: str
    Tags: dict[str, str] = Field(default_factory=dict)

    # Native FinOps & AI attribution extensions
    x_record_id: str
    x_source: str
    x_team: str = "unallocated"
    x_project_id: str = "default"
    x_model: str = "n/a"
    x_caller: str = "unknown"
    x_agent_id: str = "none"
    x_input_tokens: int = 0
    x_output_tokens: int = 0
    x_cached_tokens: int = 0
    x_total_tokens: int = 0

    def to_storage_row(self) -> dict[str, Any]:
        """Serialize record into flat primitives suitable for DuckDB/Parquet storage."""
        import json

        data = self.model_dump()
        data["Tags"] = json.dumps(data["Tags"], sort_keys=True)
        for ts_field in (
            "BillingPeriodStart",
            "BillingPeriodEnd",
            "ChargePeriodStart",
            "ChargePeriodEnd",
        ):
            data[ts_field] = data[ts_field].isoformat()
        return data


FOCUS_COLUMNS: list[str] = list(FocusRecord.model_fields.keys())
