from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, model_validator
from pymongo.collection import Collection

from app.api.deps import get_current_user, get_customer_collection, get_user_collection
from app.core.access_profile import build_user_public
from app.core.subscription_catalog import public_plans_catalog
from app.core.timezone import get_ist_timezone
from app.models.user import UserInDB, UserPublic

router = APIRouter(prefix="/subscription", tags=["subscription"])
IST = get_ist_timezone()


class SelectPlanRequest(BaseModel):
    tier: Literal["free", "basic", "pro", "elite", "unlimited"]
    billing_period: Optional[Literal["monthly", "yearly"]] = None

    @model_validator(mode="after")
    def validate_billing(self) -> "SelectPlanRequest":
        if self.tier != "free" and self.billing_period is None:
            raise ValueError("billing_period is required for paid plans")
        if self.tier == "free":
            self.billing_period = None
        return self


class SelectPlanResponse(BaseModel):
    user: UserPublic


@router.get("/plans")
def get_plans() -> dict:
    """Public catalog (no auth) so the picker can render before session is ready."""
    return public_plans_catalog()


@router.post("/select-plan", response_model=SelectPlanResponse)
def select_plan(
    payload: SelectPlanRequest,
    current_user: UserInDB = Depends(get_current_user),
    users_coll: Collection = Depends(get_user_collection),
    customers_coll: Collection = Depends(get_customer_collection),
) -> SelectPlanResponse:
    if current_user.role == "customer" and current_user.linked_admin_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the business admin can change the subscription plan.",
        )

    now = datetime.now(IST)
    if payload.tier == "free":
        expires_at: datetime | None = None
    elif payload.billing_period == "monthly":
        expires_at = now + timedelta(days=30)
    else:
        expires_at = now + timedelta(days=365)

    update_doc: dict = {
        "subscription_tier": payload.tier,
        "billing_period": None if payload.tier == "free" else payload.billing_period,
        "has_subscription": True,
        "subscription_expires_at": expires_at,
    }
    users_coll.update_one({"_id": current_user.id}, {"$set": update_doc})

    refreshed = users_coll.find_one({"_id": current_user.id})
    if refreshed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    fresh_user = UserInDB.from_mongo(refreshed)
    return SelectPlanResponse(user=build_user_public(fresh_user, users_coll, customers_coll))
