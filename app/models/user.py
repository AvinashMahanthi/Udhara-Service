from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.finance import DelayedCustomerPublic

SubscriptionTier = Literal["pending", "free", "basic", "pro", "elite", "unlimited"]
BillingPeriod = Literal["monthly", "yearly"]


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=3, description="Email address or phone number")
    password: str = Field(min_length=6)


class RegisterRequest(BaseModel):
    user_id: str = Field(min_length=3)
    full_name: str = Field(min_length=2)
    email: EmailStr
    phone_number: str = Field(min_length=8)
    password: str = Field(min_length=6)
    role: Literal["admin", "customer"] = "customer"
    linked_admin_id: Optional[str] = None
    admin_password: Optional[str] = None


class UserPublic(BaseModel):
    id: str
    full_name: str
    email: EmailStr
    phone_number: str
    role: Literal["admin", "customer"]
    has_subscription: bool = False
    linked_admin_id: Optional[str] = None
    allow_collaborators: bool = True
    subscription_tier: SubscriptionTier = "pending"
    billing_period: Optional[BillingPeriod] = None
    customer_usage_used: int = 0
    customer_usage_limit: int = 2
    subscription_expires_at: Optional[datetime] = None


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic


class DashboardCard(BaseModel):
    title: str
    value: str
    description: str


class DashboardSummaryMetric(BaseModel):
    label: str
    value: str
    trend: str
    tone: Literal["positive", "negative", "neutral"]


class DashboardDailyCard(BaseModel):
    day: str
    invested: str
    returns: str
    profit_or_loss: str
    tone: Literal["positive", "negative", "neutral"]


class DashboardFinanceBookPerformance(BaseModel):
    scope: Literal["daily", "weekly", "monthly", "yearly"]
    label: str
    invested: str
    returns: str
    profit_or_loss: str
    tone: Literal["positive", "negative", "neutral"]


class DashboardResponse(BaseModel):
    message: str
    user_id: str
    role: Literal["admin", "customer"]
    has_subscription: bool
    subscription_tier: SubscriptionTier = "pending"
    billing_period: Optional[BillingPeriod] = None
    customer_usage_used: int = 0
    customer_usage_limit: int = 2
    subscription_expires_at: Optional[datetime] = None
    summary: list[DashboardSummaryMetric]
    daily_cards: list[DashboardDailyCard]
    finance_book_performance: list[DashboardFinanceBookPerformance] = []
    attention_required_count: int = 0
    overdue_amount: str = "₹0"
    delayed_customers: list[DelayedCustomerPublic] = []


class UserInDB(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="_id")
    full_name: str
    email: EmailStr
    phone_number: str
    role: Literal["admin", "customer"]
    has_subscription: bool = False
    hashed_password: str
    linked_admin_id: Optional[str] = None
    allow_collaborators: bool = True
    session_id: Optional[str] = None
    subscription_tier: SubscriptionTier = "pending"
    billing_period: Optional[BillingPeriod] = None
    subscription_expires_at: Optional[datetime] = None

    @classmethod
    def from_mongo(cls, document: dict[str, Any]) -> "UserInDB":
        payload = {**document, "_id": str(document["_id"])}
        if "subscription_tier" not in document:
            payload["subscription_tier"] = "unlimited" if document.get("has_subscription") else "pending"
        tier = str(payload.get("subscription_tier", "pending"))
        payload["subscription_tier"] = tier  # type: ignore[assignment]
        payload["has_subscription"] = tier != "pending"
        if "billing_period" not in document:
            payload["billing_period"] = None
        if "subscription_expires_at" not in document:
            payload["subscription_expires_at"] = None
        if "allow_collaborators" not in document:
            payload["allow_collaborators"] = True
        return cls.model_validate(payload)
