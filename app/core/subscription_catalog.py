"""Customer limits and public pricing catalog (INR, monthly / yearly)."""

from __future__ import annotations

from typing import Any, Literal

SubscriptionTier = Literal["pending", "free", "basic", "pro", "elite", "unlimited"]
PaidTier = Literal["basic", "pro", "elite", "unlimited"]
BillingPeriod = Literal["monthly", "yearly"]

# Global customer count per owner (all villages + finance modes).
TIER_CUSTOMER_LIMIT: dict[str, int] = {
    "pending": 0,
    # TODO(testing): revert free limit to 30 before production.
    "free": 2,
    "basic": 500,
    "pro": 1000,
    "elite": 2000,
    "unlimited": 10_000_000,
}

# Monthly list prices (INR).
PAID_MONTHLY_INR: dict[PaidTier, int] = {
    "basic": 199,
    "pro": 299,
    "elite": 399,
    "unlimited": 499,
}

# Payment gateway charges passed on to the subscriber.
PLATFORM_FEE_PERCENT: float = 2.15   # % of base price
GST_PERCENT: float = 18.0            # % of base price (Indian GST on subscription)


def total_charge_paise(base_inr: int) -> int:
    """
    Base price + 2.15% platform fee + 18% GST, both applied on the base price.
    Total extra = 2.15 + 18 = 20.15% of base.
    This is the exact amount sent to Razorpay and verified on payment confirmation.
    """
    total = base_inr * (1 + (PLATFORM_FEE_PERCENT + GST_PERCENT) / 100)
    return round(total * 100)


def customer_limit_for_tier(tier: str) -> int:
    return TIER_CUSTOMER_LIMIT.get(tier, TIER_CUSTOMER_LIMIT["free"])


def yearly_price_inr(tier: PaidTier) -> int:
    """Yearly = 10× monthly (two months free) — simple, attractive annual anchor."""
    return PAID_MONTHLY_INR[tier] * 10


def yearly_savings_vs_monthly_x12_inr(tier: PaidTier) -> int:
    return PAID_MONTHLY_INR[tier] * 12 - yearly_price_inr(tier)


def public_plans_catalog() -> dict[str, Any]:
    """Single source of truth for the mobile plan picker."""
    paid_rows: list[dict[str, Any]] = []
    for tid in ("basic", "pro", "elite", "unlimited"):
        tier: PaidTier = tid  # type: ignore[assignment]
        monthly = PAID_MONTHLY_INR[tier]
        yearly = yearly_price_inr(tier)
        savings = yearly_savings_vs_monthly_x12_inr(tier)
        paid_rows.append(
            {
                "id": tier,
                "customer_limit": None if tier == "unlimited" else TIER_CUSTOMER_LIMIT[tier],
                "monthly_inr": monthly,
                "yearly_inr": yearly,
                "yearly_savings_inr_vs_monthly_x12": savings,
                "recommended": tier == "elite",
            }
        )
    return {
        "currency": "INR",
        "billing_note": "Yearly is billed as 10× monthly — about 17% less than paying monthly for 12 months.",
        "free": {
            "id": "free",
            "customer_limit": TIER_CUSTOMER_LIMIT["free"],
            "monthly_inr": 0,
            "yearly_inr": 0,
        },
        "paid_tiers": paid_rows,
    }
