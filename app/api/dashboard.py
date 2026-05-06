from datetime import datetime, timedelta
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pymongo.collection import Collection

from app.api.deps import get_collection_record_collection, get_current_user, get_customer_collection, get_installment_collection, get_user_collection, get_village_collection
from app.core.access_profile import subscription_usage_for_dashboard
from app.core.finance_scope import villages_mongo_filter
from app.core.timezone import get_ist_timezone
from app.models.finance import DelayedCustomerPublic
from app.models.user import DashboardDailyCard, DashboardFinanceBookPerformance, DashboardResponse, DashboardSummaryMetric, UserInDB

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_FINANCE_SCOPES = frozenset({"all", "daily", "weekly", "monthly", "yearly"})
IST = get_ist_timezone()
MONTHLY_YEARLY_INTEREST_GRACE_DAYS = 5


def _now_ist() -> datetime:
    return datetime.now(IST)


def _due_date_ist(due_value: datetime) -> datetime:
    if due_value.tzinfo is None:
        return due_value.replace(tzinfo=IST)
    return due_value.astimezone(IST)


def _installment_due_date(document: dict) -> date:
    due = document["due_date"]
    if isinstance(due, datetime):
        return _due_date_ist(due).date()
    return due


def _daily_interest_amount(interest_type: str | None, interest_value: float, balance: float) -> float:
    if not interest_type or interest_value <= 0 or balance <= 0:
        return 0.0
    if interest_type == "percentage":
        return round(balance * (interest_value / 100), 2)
    if interest_type in {"rupees_100", "daily_rupees"}:
        return round((balance / 100) * interest_value, 2)
    if interest_type == "rupees":
        return round((balance / 1000) * interest_value, 2)
    return 0.0


def _apply_interest_for_customer(customer: dict, installment_collection: Collection, now: datetime) -> None:
    interest_type = customer.get("interest_type")
    interest_value = float(customer.get("interest_value", 0) or 0)
    if interest_type not in {"percentage", "rupees"} or interest_value <= 0:
        return

    today = now.date()
    installments = list(
        installment_collection.find(
            {"owner_user_id": customer["owner_user_id"], "customer_id": str(customer["_id"])},
        ).sort("due_date", 1)
    )
    payment_type = str(customer.get("payment_type", "weekly"))
    if payment_type in {"daily", "weekly"}:
        unpaid_installments = [
            item
            for item in installments
            if item.get("status") != "paid"
            and max(float(item.get("amount_due", 0)) - float(item.get("amount_paid", 0)), 0) > 0
        ]
        if not unpaid_installments:
            return
        final_due_date = max(_installment_due_date(item) for item in installments)
        if today <= final_due_date:
            return
        target = max(unpaid_installments, key=_installment_due_date)
        last_applied = target.get("last_interest_applied_date")
        if isinstance(last_applied, datetime):
            last_interest_date = _due_date_ist(last_applied).date()
        elif isinstance(last_applied, date):
            last_interest_date = last_applied
        else:
            last_interest_date = final_due_date
        days_elapsed = (today - last_interest_date).days
        if days_elapsed <= 0:
            return
        outstanding_balance = sum(
            max(float(item.get("amount_due", 0)) - float(item.get("amount_paid", 0)), 0)
            for item in unpaid_installments
        )
        interest_to_add = round(_daily_interest_amount(interest_type, interest_value, outstanding_balance) * days_elapsed, 2)
        if interest_to_add <= 0:
            return
        set_fields = {"last_interest_applied_date": today}
        if target.get("principal_due") is None:
            set_fields["principal_due"] = float(target.get("amount_due", 0))
        installment_collection.update_one(
            {"_id": target["_id"], "owner_user_id": customer["owner_user_id"]},
            {
                "$inc": {"amount_due": interest_to_add, "interest_added": interest_to_add},
                "$set": set_fields,
            },
        )
        return

    for installment in installments:
        if installment.get("status") == "paid":
            continue
        balance = max(float(installment.get("amount_due", 0)) - float(installment.get("amount_paid", 0)), 0)
        if balance <= 0:
            continue
        due_date = _installment_due_date(installment)
        last_applied = installment.get("last_interest_applied_date")
        if isinstance(last_applied, datetime):
            last_interest_date = _due_date_ist(last_applied).date()
        elif isinstance(last_applied, date):
            last_interest_date = last_applied
        else:
            last_interest_date = due_date
        interest_start_date = due_date + timedelta(days=MONTHLY_YEARLY_INTEREST_GRACE_DAYS)
        if today <= interest_start_date:
            continue
        start_date = max(last_interest_date, interest_start_date)
        days_elapsed = (today - start_date).days
        if days_elapsed <= 0:
            continue
        interest_to_add = round(_daily_interest_amount(interest_type, interest_value, balance) * days_elapsed, 2)
        if interest_to_add <= 0:
            continue
        target = next(
            (
                item
                for item in installments
                if _installment_due_date(item) > due_date and item.get("status") in {"pending", "partial"}
            ),
            installment,
        )
        set_fields = {}
        if target.get("principal_due") is None:
            set_fields["principal_due"] = float(target.get("amount_due", 0))
        installment_collection.update_one(
            {"_id": target["_id"], "owner_user_id": customer["owner_user_id"]},
            {
                "$inc": {"amount_due": interest_to_add, "interest_added": interest_to_add},
                **({"$set": set_fields} if set_fields else {}),
            },
        )
        installment_collection.update_one(
            {"_id": installment["_id"], "owner_user_id": customer["owner_user_id"]},
            {"$set": {"last_interest_applied_date": today}},
        )


def _format_currency(value: int) -> str:
    amount = abs(int(round(value)))
    digits = str(amount)

    if len(digits) > 3:
        last_three = digits[-3:]
        remaining = digits[:-3]
        groups: list[str] = []
        while len(remaining) > 2:
            groups.insert(0, remaining[-2:])
            remaining = remaining[:-2]
        if remaining:
            groups.insert(0, remaining)
        digits = ",".join([*groups, last_three])

    sign = "-" if value < 0 else ""
    return f"{sign}₹{digits}"



def _build_daily_values(
    village_collection: Collection,
    customer_collection: Collection,
    _installment_collection: Collection,
    collection_record_collection: Collection,
    owner_id: str,
    finance_scope: str,
) -> tuple[list[DashboardDailyCard], int, int]:
    village_query = {"owner_user_id": owner_id} if finance_scope == "all" else villages_mongo_filter(owner_id, finance_scope)
    village_documents = list(village_collection.find(village_query, {"_id": 1, "day": 1}))
    village_ids = [str(v["_id"]) for v in village_documents]
    if not village_ids:
        customer_documents: list[dict] = []
        collection_documents: list[dict] = []
    else:
        customer_documents = list(
            customer_collection.find(
                {"owner_user_id": owner_id, "village_id": {"$in": village_ids}},
                {"_id": 1, "village_id": 1, "amount_lent": 1},
            )
        )
        customer_ids = [str(c["_id"]) for c in customer_documents]
        collection_documents = (
            list(
                collection_record_collection.find(
                    {"owner_user_id": owner_id, "customer_id": {"$in": customer_ids}},
                    {"customer_id": 1, "collection_batch_id": 1, "amount_paid": 1, "batch_total_amount": 1},
                )
            )
            if customer_ids
            else []
        )
    daily_map: dict[str, dict[str, float]] = {day: {"invested": 0.0, "returns": 0.0} for day in DAYS}
    village_day_by_id = {str(document["_id"]): document.get("day", "") for document in village_documents}
    customer_day_by_id: dict[str, str] = {}

    for document in customer_documents:
        day = village_day_by_id.get(str(document.get("village_id")), "")
        customer_id = str(document.get("_id"))
        customer_day_by_id[customer_id] = day
        if day in daily_map:
            daily_map[day]["invested"] += float(document.get("amount_lent", 0))

    counted_batches: set[str] = set()
    for document in collection_documents:
        batch_id = str(document.get("collection_batch_id", document.get("_id", "")))
        if not batch_id or batch_id in counted_batches:
            continue
        day = customer_day_by_id.get(str(document.get("customer_id")), "")
        if day in daily_map:
            daily_map[day]["returns"] += float(document.get("batch_total_amount", document.get("amount_paid", 0)) or 0)
        counted_batches.add(batch_id)

    invested_total = round(sum(values["invested"] for values in daily_map.values()))
    returns_total = round(sum(values["returns"] for values in daily_map.values()))

    cards: list[DashboardDailyCard] = []
    for day in DAYS:
        invested = round(daily_map[day]["invested"])
        returns = round(daily_map[day]["returns"])
        difference = returns - invested
        tone = "positive" if difference > 0 else "negative" if difference < 0 else "neutral"
        sign = "+" if difference > 0 else ""
        cards.append(
            DashboardDailyCard(
                day=day,
                invested=_format_currency(invested),
                returns=_format_currency(returns),
                profit_or_loss=f"{sign}{_format_currency(difference)}",
                tone=tone,
            )
        )
    return cards, invested_total, returns_total


def _build_summary(invested_total: int, returns_total: int) -> list[DashboardSummaryMetric]:
    profit_total = returns_total - invested_total
    margin = profit_total / invested_total if invested_total else 0
    profit_tone = "positive" if profit_total > 0 else "negative" if profit_total < 0 else "neutral"
    sign = "+" if profit_total > 0 else ""

    return [
        DashboardSummaryMetric(
            label="Amount invested",
            value=_format_currency(invested_total),
            trend="Total principal across all days",
            tone="neutral",
        ),
        DashboardSummaryMetric(
            label="Amount in returns",
            value=_format_currency(returns_total),
            trend="Total collected across all days",
            tone=profit_tone,
        ),
        DashboardSummaryMetric(
            label="Profit or loss",
            value=f"{sign}{_format_currency(profit_total)}",
            trend=f"Margin {margin * 100:.1f}%",
            tone=profit_tone,
        ),
    ]


def _build_finance_book_performance(
    village_collection: Collection,
    customer_collection: Collection,
    installment_collection: Collection,
    collection_record_collection: Collection,
    owner_id: str,
) -> list[DashboardFinanceBookPerformance]:
    labels = {
        "daily": "Daily Finance",
        "weekly": "Weekly Finance",
        "monthly": "Monthly Finance",
        "yearly": "Yearly Finance",
    }
    rows: list[DashboardFinanceBookPerformance] = []
    for scope in ("daily", "weekly", "monthly", "yearly"):
        _, invested_total, returns_total = _build_daily_values(
            village_collection,
            customer_collection,
            installment_collection,
            collection_record_collection,
            owner_id,
            scope,
        )
        profit_total = returns_total - invested_total
        tone = "positive" if profit_total > 0 else "negative" if profit_total < 0 else "neutral"
        sign = "+" if profit_total > 0 else ""
        rows.append(
            DashboardFinanceBookPerformance(
                scope=scope,  # type: ignore[arg-type]
                label=labels[scope],
                invested=_format_currency(invested_total),
                returns=_format_currency(returns_total),
                profit_or_loss=f"{sign}{_format_currency(profit_total)}",
                tone=tone,
            )
        )
    return rows


def _build_delay_summary(
    village_collection: Collection,
    customer_collection: Collection,
    installment_collection: Collection,
    collection_record_collection: Collection,
    owner_id: str,
    finance_scope: str,
) -> tuple[int, int, list[DelayedCustomerPublic]]:
    now = _now_ist()
    village_query = {"owner_user_id": owner_id} if finance_scope == "all" else villages_mongo_filter(owner_id, finance_scope)
    villages = list(village_collection.find(village_query, {"_id": 1, "name": 1}))
    village_ids = [str(v["_id"]) for v in villages]
    if not village_ids:
        customers: list[dict] = []
    else:
        customers = list(
            customer_collection.find(
                {"owner_user_id": owner_id, "village_id": {"$in": village_ids}},
                {"_id": 1, "full_name": 1, "phone_number": 1, "village_id": 1},
            )
        )
    customer_ids = [str(customer["_id"]) for customer in customers]
    village_name_by_id = {str(village["_id"]): village.get("name", "Unknown village") for village in villages}
    last_collection_by_customer: dict[str, dict] = {}
    delayed_map = {
        customer_id: {
            "overdue_installments": 0,
            "overdue_amount": 0.0,
        }
        for customer_id in customer_ids
    }

    if customer_ids:
        for record in collection_record_collection.find(
            {"owner_user_id": owner_id, "customer_id": {"$in": customer_ids}},
            {"customer_id": 1, "collected_at": 1, "collected_by_name": 1},
        ).sort("collected_at", -1):
            last_collection_by_customer.setdefault(record["customer_id"], record)

        for installment in installment_collection.find(
            {"owner_user_id": owner_id, "customer_id": {"$in": customer_ids}},
            {"customer_id": 1, "amount_due": 1, "amount_paid": 1, "due_date": 1, "status": 1},
        ):
            if installment.get("status") == "paid" or _due_date_ist(installment["due_date"]).date() >= now.date():
                continue
            delayed_map[installment["customer_id"]]["overdue_installments"] += 1
            delayed_map[installment["customer_id"]]["overdue_amount"] += max(
                float(installment.get("amount_due", 0)) - float(installment.get("amount_paid", 0)),
                0,
            )

    delayed_customers: list[DelayedCustomerPublic] = []
    total_overdue_amount = 0
    attention_required_count = 0
    for customer in customers:
        customer_id = str(customer["_id"])
        overdue_installments = int(delayed_map[customer_id]["overdue_installments"])
        overdue_amount = round(float(delayed_map[customer_id]["overdue_amount"]))
        if overdue_installments <= 0:
            continue
        attention_required_count += 1
        total_overdue_amount += overdue_amount
        last_collection = last_collection_by_customer.get(customer_id)
        delayed_customers.append(
            DelayedCustomerPublic(
                customer_id=customer_id,
                full_name=customer.get("full_name", "Unknown customer"),
                village_name=village_name_by_id.get(str(customer.get("village_id")), "Unknown village"),
                phone_number=customer.get("phone_number", ""),
                overdue_installments=overdue_installments,
                overdue_amount=overdue_amount,
                last_collected_at=last_collection.get("collected_at") if last_collection else None,
                last_collected_by_name=last_collection.get("collected_by_name") if last_collection else None,
            )
        )

    delayed_customers.sort(key=lambda item: (item.overdue_amount, item.overdue_installments), reverse=True)
    return attention_required_count, total_overdue_amount, delayed_customers[:6]


@router.get("", response_model=DashboardResponse)
def dashboard(
    current_user: UserInDB = Depends(get_current_user),
    user_collection: Collection = Depends(get_user_collection),
    village_collection: Collection = Depends(get_village_collection),
    customer_collection: Collection = Depends(get_customer_collection),
    installment_collection: Collection = Depends(get_installment_collection),
    collection_record_collection: Collection = Depends(get_collection_record_collection),
    finance_scope: str = Query(default="all", description="Workspace: all|daily|weekly|monthly|yearly"),
) -> DashboardResponse:
    if finance_scope not in _FINANCE_SCOPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid finance_scope")
    owner_id = current_user.linked_admin_id if (current_user.role == "customer" and current_user.linked_admin_id) else current_user.id
    daily_cards, invested_total, returns_total = _build_daily_values(
        village_collection,
        customer_collection,
        installment_collection,
        collection_record_collection,
        owner_id,
        finance_scope,
    )
    attention_required_count, overdue_amount, delayed_customers = _build_delay_summary(
        village_collection,
        customer_collection,
        installment_collection,
        collection_record_collection,
        owner_id,
        finance_scope,
    )
    finance_book_performance = _build_finance_book_performance(
        village_collection,
        customer_collection,
        installment_collection,
        collection_record_collection,
        owner_id,
    )

    usage = subscription_usage_for_dashboard(current_user, user_collection, customer_collection)
    return DashboardResponse(
        message=f"Welcome back, {current_user.full_name}.",
        user_id=current_user.id,
        role=current_user.role,
        has_subscription=usage["has_subscription"],
        subscription_tier=usage["subscription_tier"],
        billing_period=usage["billing_period"],
        customer_usage_used=usage["customer_usage_used"],
        customer_usage_limit=usage["customer_usage_limit"],
        subscription_expires_at=usage["subscription_expires_at"],
        summary=_build_summary(invested_total, returns_total),
        daily_cards=daily_cards,
        finance_book_performance=finance_book_performance,
        attention_required_count=attention_required_count,
        overdue_amount=_format_currency(overdue_amount),
        delayed_customers=delayed_customers,
    )