import calendar
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pymongo.collection import Collection

from app.api.deps import (
    get_collection_record_collection,
    get_current_user,
    get_customer_collection,
    get_installment_collection,
    get_village_collection,
)
from app.core.access_profile import subscription_is_active
from app.core.finance_scope import villages_mongo_filter
from app.core.timezone import get_ist_timezone
from app.models.finance import (
    CollectionRecordCreate,
    CollectionRecordPublic,
    CollectionTimeseriesPoint,
    CollectionTransactionRow,
    CollectionsReportFacetCustomer,
    CollectionsReportFacetVillage,
    CollectionsReportFacets,
    CollectionsReportResponse,
    CustomerCreate,
    CustomerDetailResponse,
    CustomerPublic,
    CustomerUpdate,
    DelayedCustomerPublic,
    InstallmentCalendarEntry,
    VillageCreate,
    VillagePublic,
    VillageUpdate,
)
from app.core.subscription_catalog import customer_limit_for_tier
from app.models.user import UserInDB

router = APIRouter(tags=["finance"])

_VALID_PAYMENT_MODES = ("phonepe", "gpay", "cash")
IST = get_ist_timezone()
MONTHLY_YEARLY_INTEREST_GRACE_DAYS = 5


def _now_ist() -> datetime:
    return datetime.now(IST)


def _as_ist(dt: datetime | date) -> datetime:
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return datetime.combine(dt, time.min, tzinfo=IST)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _effective_owner_id(user: UserInDB) -> str:
    """Collaborators (role=customer with linked_admin_id) see their admin's data."""
    if user.role == "customer" and user.linked_admin_id:
        return user.linked_admin_id
    return user.id


def _require_write_access(user: UserInDB) -> None:
    """Block write/delete operations for collaborator (Normal user) accounts."""
    if user.role == "customer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Collaborator accounts can view data but cannot create, edit or delete.",
        )

def _village_finance_scope(document: dict) -> str:
    raw = document.get("finance_scope")
    if raw in ("daily", "weekly", "monthly", "yearly"):
        return raw
    return "weekly"


def _serialize_village(document: dict, customer_count: int = 0) -> VillagePublic:
    return VillagePublic(
        id=str(document["_id"]),
        owner_user_id=document["owner_user_id"],
        name=document["name"],
        day=document["day"],
        finance_scope=_village_finance_scope(document),
        customer_count=customer_count,
    )


def _serialize_customer(document: dict) -> CustomerPublic:
    return CustomerPublic(
        id=str(document["_id"]),
        owner_user_id=document["owner_user_id"],
        village_id=document["village_id"],
        full_name=document["full_name"],
        address=document["address"],
        amount_lent=float(document["amount_lent"]),
        payment_type=document["payment_type"],
        installment_amount=float(document["installment_amount"]),
        installment_count=int(document["installment_count"]),
        phone_number=document["phone_number"],
        image_url=document.get("image_url"),
        aadhar_number=document["aadhar_number"],
        aadhar_image_url=document.get("aadhar_image_url"),
        external_customer_id=document["external_customer_id"],
        interest_type=document.get("interest_type"),
        interest_value=float(document.get("interest_value", 0) or 0),
        overdue_installments=int(document.get("overdue_installments", 0)),
        overdue_amount=float(document.get("overdue_amount", 0)),
        due_this_month_installments=int(document.get("due_this_month_installments", 0)),
        due_this_month_amount=float(document.get("due_this_month_amount", 0)),
        due_this_year_installments=int(document.get("due_this_year_installments", 0)),
        due_this_year_amount=float(document.get("due_this_year_amount", 0)),
        due_today_installments=int(document.get("due_today_installments", 0)),
        due_today_amount=float(document.get("due_today_amount", 0)),
        total_collected=float(document.get("total_collected", 0)),
        last_collected_at=document.get("last_collected_at"),
        last_collected_by_name=document.get("last_collected_by_name"),
    )


def _serialize_collection_record(document: dict) -> CollectionRecordPublic:
    return CollectionRecordPublic(
        id=str(document["_id"]),
        collection_batch_id=document.get("collection_batch_id", str(document["_id"])),
        batch_anchor_installment_id=document.get("batch_anchor_installment_id", document["installment_id"]),
        owner_user_id=document["owner_user_id"],
        village_id=document["village_id"],
        customer_id=document["customer_id"],
        installment_id=document["installment_id"],
        amount_paid=float(document.get("batch_total_amount", document["amount_paid"])),
        covered_installment_count=int(document.get("covered_installment_count", 1)),
        covered_installment_ids=[str(item) for item in document.get("covered_installment_ids", [document["installment_id"]])],
        payment_mode=document["payment_mode"],
        collected_by_user_id=document["collected_by_user_id"],
        collected_by_name=document["collected_by_name"],
        collected_at=document["collected_at"],
        status_after_payment=document["status_after_payment"],
        note=document.get("note"),
        late_interest_base_amount=float(document.get("late_interest_base_amount", 0) or 0),
        late_interest_type=document.get("late_interest_type"),
        late_interest_value=float(document.get("late_interest_value", 0) or 0),
        late_interest_from=document.get("late_interest_from"),
        late_interest_to=document.get("late_interest_to"),
        late_interest_days=int(document.get("late_interest_days", 0) or 0),
        late_interest_amount=float(document.get("late_interest_amount", 0) or 0),
        late_interest_collected_amount=float(document.get("late_interest_collected_amount", document.get("late_interest_amount", 0)) or 0),
    )


def _group_collection_history(collection_history: list[dict]) -> list[CollectionRecordPublic]:
    grouped: dict[str, dict] = {}

    for record in collection_history:
        batch_id = record.get("collection_batch_id", str(record["_id"]))
        if batch_id not in grouped:
            grouped[batch_id] = {
                **record,
                "covered_installment_ids": [],
                "covered_installment_count": 0,
                "batch_total_amount": 0.0,
                "late_interest_base_amount": 0.0,
                "late_interest_type": None,
                "late_interest_value": 0.0,
                "late_interest_from": None,
                "late_interest_to": None,
                "late_interest_days": 0,
                "late_interest_amount": 0.0,
                "late_interest_collected_amount": 0.0,
            }

        grouped_record = grouped[batch_id]
        installment_id = str(record["installment_id"])
        if installment_id not in grouped_record["covered_installment_ids"]:
            grouped_record["covered_installment_ids"].append(installment_id)
            grouped_record["covered_installment_count"] += 1
        grouped_record["batch_total_amount"] += float(record["amount_paid"])
        record_late_interest = float(record.get("late_interest_amount", 0) or 0)
        if record_late_interest > float(grouped_record.get("late_interest_amount", 0) or 0):
            grouped_record["late_interest_base_amount"] = float(record.get("late_interest_base_amount", 0) or 0)
            grouped_record["late_interest_type"] = record.get("late_interest_type")
            grouped_record["late_interest_value"] = float(record.get("late_interest_value", 0) or 0)
            grouped_record["late_interest_from"] = record.get("late_interest_from")
            grouped_record["late_interest_to"] = record.get("late_interest_to")
            grouped_record["late_interest_days"] = int(record.get("late_interest_days", 0) or 0)
            grouped_record["late_interest_amount"] = record_late_interest
            grouped_record["late_interest_collected_amount"] = float(
                record.get("late_interest_collected_amount", record_late_interest) or 0
            )
            grouped_record["batch_total_amount"] += float(grouped_record["late_interest_collected_amount"])

    sorted_grouped_records = sorted(
        grouped.values(),
        key=lambda item: item.get("collected_at") or datetime.min.replace(tzinfo=IST),
        reverse=True,
    )
    return [_serialize_collection_record(grouped_record) for grouped_record in sorted_grouped_records]


def _installment_due_date(document: dict) -> date:
    due = document["due_date"]
    if isinstance(due, datetime):
        return _as_ist(due).date()
    return due


def _is_installment_overdue(document: dict, now: datetime) -> bool:
    return document.get("status") != "paid" and _installment_due_date(document) < now.date()


def _build_customer_metrics(
    owner_id: str,
    customer_ids: list[str],
    installment_collection: Collection,
    collection_record_collection: Collection,
) -> dict[str, dict[str, object]]:
    metrics = {
        customer_id: {
            "overdue_installments": 0,
            "overdue_amount": 0.0,
            "due_this_month_installments": 0,
            "due_this_month_amount": 0.0,
            "due_this_year_installments": 0,
            "due_this_year_amount": 0.0,
            "due_today_installments": 0,
            "due_today_amount": 0.0,
            "total_collected": 0.0,
            "last_collected_at": None,
            "last_collected_by_name": None,
        }
        for customer_id in customer_ids
    }
    if not customer_ids:
        return metrics

    now = _now_ist()
    installment_documents = list(
        installment_collection.find(
            {"owner_user_id": owner_id, "customer_id": {"$in": customer_ids}},
            {"customer_id": 1, "amount_due": 1, "amount_paid": 1, "due_date": 1, "status": 1},
        )
    )
    collection_documents = list(
        collection_record_collection.find(
            {"owner_user_id": owner_id, "customer_id": {"$in": customer_ids}},
            {
                "customer_id": 1,
                "collection_batch_id": 1,
                "late_interest_amount": 1,
                "late_interest_collected_amount": 1,
                "collected_at": 1,
                "collected_by_name": 1,
            },
        ).sort("collected_at", -1)
    )

    for installment in installment_documents:
        customer_id = installment["customer_id"]
        metric = metrics.get(customer_id)
        if metric is None:
            continue
        amount_due = float(installment.get("amount_due", 0))
        amount_paid = float(installment.get("amount_paid", 0))
        metric["total_collected"] = float(metric["total_collected"]) + amount_paid
        if _is_installment_overdue(installment, now):
            metric["overdue_installments"] = int(metric["overdue_installments"]) + 1
            metric["overdue_amount"] = float(metric["overdue_amount"]) + max(amount_due - amount_paid, 0)

        status = str(installment.get("status", "pending"))
        remaining = max(amount_due - amount_paid, 0)
        if remaining > 0 and status in {"pending", "partial"}:
            d_only = _installment_due_date(installment)
            if d_only.year == now.year and d_only.month == now.month:
                metric["due_this_month_installments"] = int(metric["due_this_month_installments"]) + 1
                metric["due_this_month_amount"] = float(metric["due_this_month_amount"]) + remaining
            if d_only.year == now.year:
                metric["due_this_year_installments"] = int(metric["due_this_year_installments"]) + 1
                metric["due_this_year_amount"] = float(metric["due_this_year_amount"]) + remaining
            if d_only == now.date():
                metric["due_today_installments"] = int(metric["due_today_installments"]) + 1
                metric["due_today_amount"] = float(metric["due_today_amount"]) + remaining

    counted_interest_batches: set[str] = set()
    for record in collection_documents:
        customer_id = record["customer_id"]
        metric = metrics.get(customer_id)
        if metric is None:
            continue

        batch_id = str(record.get("collection_batch_id", record.get("_id", "")))
        if batch_id and batch_id not in counted_interest_batches:
            metric["total_collected"] = float(metric["total_collected"]) + float(
                record.get("late_interest_collected_amount", record.get("late_interest_amount", 0)) or 0
            )
            counted_interest_batches.add(batch_id)

        if metric["last_collected_at"] is not None:
            continue
        metric["last_collected_at"] = record.get("collected_at")
        metric["last_collected_by_name"] = record.get("collected_by_name")

    return metrics


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


def _manual_late_interest_amount(payload: CollectionRecordCreate) -> tuple[int, float]:
    if (
        payload.late_interest_type is None
        or payload.late_interest_base_amount <= 0
        or payload.late_interest_value <= 0
        or payload.late_interest_from is None
        or payload.late_interest_to is None
    ):
        return 0, 0.0

    days = max((payload.late_interest_to - payload.late_interest_from).days, 0)
    daily_amount = _daily_interest_amount(
        payload.late_interest_type,
        float(payload.late_interest_value),
        float(payload.late_interest_base_amount),
    )
    return days, round(daily_amount * days, 2)


def _apply_interest_for_customer(
    customer: dict,
    installment_collection: Collection,
    now: datetime | None = None,
) -> None:
    interest_type = customer.get("interest_type")
    interest_value = float(customer.get("interest_value", 0) or 0)
    if interest_type not in {"percentage", "rupees"} or interest_value <= 0:
        return

    now = now or _now_ist()
    today = now.date()
    installments = list(
        installment_collection.find(
            {"owner_user_id": customer["owner_user_id"], "customer_id": str(customer["_id"])},
        ).sort("due_date", 1)
    )
    if not installments:
        return

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
            last_interest_date = _as_ist(last_applied).date()
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
            last_interest_date = _as_ist(last_applied).date()
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


def _enrich_customer(document: dict, metrics: dict[str, object] | None = None) -> dict:
    payload = dict(document)
    metrics = metrics or {}
    payload["overdue_installments"] = int(metrics.get("overdue_installments", 0))
    payload["overdue_amount"] = float(metrics.get("overdue_amount", 0))
    payload["due_this_month_installments"] = int(metrics.get("due_this_month_installments", 0))
    payload["due_this_month_amount"] = float(metrics.get("due_this_month_amount", 0))
    payload["due_this_year_installments"] = int(metrics.get("due_this_year_installments", 0))
    payload["due_this_year_amount"] = float(metrics.get("due_this_year_amount", 0))
    payload["due_today_installments"] = int(metrics.get("due_today_installments", 0))
    payload["due_today_amount"] = float(metrics.get("due_today_amount", 0))
    payload["total_collected"] = float(metrics.get("total_collected", 0))
    payload["last_collected_at"] = metrics.get("last_collected_at")
    payload["last_collected_by_name"] = metrics.get("last_collected_by_name")
    return payload


def _build_calendar_entries(
    installments: list[dict],
    latest_collection_by_installment: dict[str, dict],
    latest_anchor_collection_by_installment: dict[str, dict],
    now: datetime,
) -> list[InstallmentCalendarEntry]:
    calendar: list[InstallmentCalendarEntry] = []
    carried_balance = 0.0

    for item in installments:
        installment_id = str(item["_id"])
        own_remaining = max(float(item["amount_due"]) - float(item.get("amount_paid", 0)), 0)
        if item.get("status") != "paid":
            carried_balance += own_remaining
        else:
            carried_balance = max(carried_balance - float(item["amount_due"]), 0)

        latest_record = latest_collection_by_installment.get(installment_id, {})
        latest_anchor_record = latest_anchor_collection_by_installment.get(installment_id, {})
        display_record = latest_record or latest_anchor_record
        calendar.append(
            InstallmentCalendarEntry(
                installment_id=installment_id,
                due_date=_as_ist(item["due_date"]).date(),
                amount_due=float(item["amount_due"]),
                amount_paid=float(item.get("amount_paid", 0)),
                amount_remaining=carried_balance if item.get("status") != "paid" else 0,
                status=item["status"],
                is_overdue=_is_installment_overdue(item, now),
                last_payment_mode=display_record.get("payment_mode"),
                last_collected_at=display_record.get("collected_at"),
                last_collected_by_name=display_record.get("collected_by_name"),
                latest_payment_event_amount=float(latest_anchor_record.get("batch_total_amount")) if latest_anchor_record.get("batch_total_amount") is not None else None,
                latest_payment_cover_count=int(latest_anchor_record.get("covered_installment_count")) if latest_anchor_record.get("covered_installment_count") is not None else None,
                principal_due=float(item.get("principal_due", item.get("amount_due", 0))),
                interest_added=float(item.get("interest_added", 0) or 0),
                last_interest_applied_date=_as_ist(item["last_interest_applied_date"]).date() if item.get("last_interest_applied_date") else None,
            )
        )

    return calendar


def _add_months(base: datetime, months_to_add: int) -> datetime:
    month_index = (base.month - 1) + months_to_add
    target_year = base.year + (month_index // 12)
    target_month = (month_index % 12) + 1
    max_day = calendar.monthrange(target_year, target_month)[1]
    target_day = min(base.day, max_day)
    return base.replace(year=target_year, month=target_month, day=target_day)


def _add_years(base: datetime, years_to_add: int) -> datetime:
    target_year = base.year + years_to_add
    max_day = calendar.monthrange(target_year, base.month)[1]
    target_day = min(base.day, max_day)
    return base.replace(year=target_year, day=target_day)


def _compute_installment_due_date(base: datetime, payment_type: str, installment_number: int) -> datetime:
    """
    Build due dates from the *next* cycle.
    installment_number is 1-based.
    """
    if payment_type == "daily":
        return base + timedelta(days=installment_number)
    if payment_type == "weekly":
        return base + timedelta(days=7 * installment_number)
    if payment_type == "monthly":
        return _add_months(base, installment_number)
    return _add_years(base, installment_number)


def _create_installments_for_customer(
    owner_user_id: str,
    village_id: str,
    customer_id: str,
    payment_type: str,
    installment_amount: float,
    installment_count: int,
    installment_collection: Collection,
) -> None:
    now = _now_ist()
    documents = []

    for index in range(installment_count):
        installment_number = index + 1
        documents.append(
            {
                "_id": f"ins-{uuid4().hex[:16]}",
                "owner_user_id": owner_user_id,
                "customer_id": customer_id,
                "village_id": village_id,
                "due_date": _compute_installment_due_date(now, payment_type, installment_number),
                "amount_due": installment_amount,
                "principal_due": installment_amount,
                "interest_added": 0.0,
                "last_interest_applied_date": None,
                "amount_paid": 0.0,
                "status": "pending",
            }
        )

    if documents:
        installment_collection.insert_many(documents)


_FINANCE_SCOPES = frozenset({"daily", "weekly", "monthly", "yearly"})


@router.get("/villages", response_model=list[VillagePublic])
def list_villages(
    current_user: UserInDB = Depends(get_current_user),
    village_collection: Collection = Depends(get_village_collection),
    customer_collection: Collection = Depends(get_customer_collection),
    finance_scope: str = Query(default="weekly", description="Workspace filter: daily|weekly|monthly|yearly"),
) -> list[VillagePublic]:
    owner_id = _effective_owner_id(current_user)
    if finance_scope not in _FINANCE_SCOPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid finance_scope")
    villages = list(village_collection.find(villages_mongo_filter(owner_id, finance_scope)).sort("day", 1))
    results: list[VillagePublic] = []

    for village in villages:
        customer_count = customer_collection.count_documents({
            "owner_user_id": owner_id,
            "village_id": str(village["_id"]),
        })
        results.append(_serialize_village(village, customer_count))

    return results


@router.post("/villages", response_model=VillagePublic, status_code=status.HTTP_201_CREATED)
def create_village(
    payload: VillageCreate,
    current_user: UserInDB = Depends(get_current_user),
    village_collection: Collection = Depends(get_village_collection),
) -> VillagePublic:
    _require_write_access(current_user)
    now = _now_ist()
    document = {
        "_id": f"vil-{uuid4().hex[:12]}",
        "owner_user_id": current_user.id,
        "name": payload.name.strip(),
        "day": payload.day.strip(),
        "finance_scope": payload.finance_scope,
        "created_at": now,
        "updated_at": now,
    }
    village_collection.insert_one(document)
    return _serialize_village(document)


@router.put("/villages/{village_id}", response_model=VillagePublic)
def update_village(
    village_id: str,
    payload: VillageUpdate,
    current_user: UserInDB = Depends(get_current_user),
    village_collection: Collection = Depends(get_village_collection),
    customer_collection: Collection = Depends(get_customer_collection),
) -> VillagePublic:
    _require_write_access(current_user)
    result = village_collection.find_one_and_update(
        {"_id": village_id, "owner_user_id": current_user.id},
        {"$set": {"name": payload.name.strip(), "day": payload.day.strip(), "updated_at": _now_ist()}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Village not found")
    customer_count = customer_collection.count_documents({"owner_user_id": current_user.id, "village_id": village_id})
    return _serialize_village(result, customer_count)


@router.delete("/villages/{village_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_village(
    village_id: str,
    current_user: UserInDB = Depends(get_current_user),
    village_collection: Collection = Depends(get_village_collection),
    customer_collection: Collection = Depends(get_customer_collection),
    installment_collection: Collection = Depends(get_installment_collection),
) -> None:
    _require_write_access(current_user)
    customer_ids = [doc["_id"] for doc in customer_collection.find({"owner_user_id": current_user.id, "village_id": village_id}, {"_id": 1})]
    if customer_ids:
        installment_collection.delete_many({"owner_user_id": current_user.id, "customer_id": {"$in": customer_ids}})
        customer_collection.delete_many({"owner_user_id": current_user.id, "village_id": village_id})

    result = village_collection.delete_one({"_id": village_id, "owner_user_id": current_user.id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Village not found")


@router.get("/villages/{village_id}/customers", response_model=list[CustomerPublic])
def list_customers_for_village(
    village_id: str,
    current_user: UserInDB = Depends(get_current_user),
    customer_collection: Collection = Depends(get_customer_collection),
    installment_collection: Collection = Depends(get_installment_collection),
    collection_record_collection: Collection = Depends(get_collection_record_collection),
) -> list[CustomerPublic]:
    owner_id = _effective_owner_id(current_user)
    documents = list(customer_collection.find({"owner_user_id": owner_id, "village_id": village_id}).sort("created_at", -1))
    metrics_by_customer = _build_customer_metrics(
        owner_id,
        [str(document["_id"]) for document in documents],
        installment_collection,
        collection_record_collection,
    )
    return [_serialize_customer(_enrich_customer(document, metrics_by_customer.get(str(document["_id"])))) for document in documents]


@router.post("/villages/{village_id}/customers", response_model=CustomerPublic, status_code=status.HTTP_201_CREATED)
def create_customer(
    village_id: str,
    payload: CustomerCreate,
    current_user: UserInDB = Depends(get_current_user),
    village_collection: Collection = Depends(get_village_collection),
    customer_collection: Collection = Depends(get_customer_collection),
    installment_collection: Collection = Depends(get_installment_collection),
) -> CustomerPublic:
    _require_write_access(current_user)
    village = village_collection.find_one({"_id": village_id, "owner_user_id": current_user.id})
    if village is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Village not found")

    if not subscription_is_active(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "An active subscription is required to add customers.",
                "code": "SUBSCRIPTION_REQUIRED",
            },
        )
    limit = customer_limit_for_tier(current_user.subscription_tier)
    existing_count = customer_collection.count_documents({"owner_user_id": current_user.id})
    if existing_count >= limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "You have reached the customer limit for your plan. Upgrade to add more customers.",
                "code": "CUSTOMER_LIMIT_REACHED",
                "used": existing_count,
                "limit": limit,
            },
        )

    now = _now_ist()
    customer_id = f"cus-{uuid4().hex[:12]}"
    document = {
        "_id": customer_id,
        "owner_user_id": current_user.id,
        "village_id": village_id,
        "full_name": payload.full_name.strip(),
        "address": payload.address.strip(),
        "amount_lent": float(payload.amount_lent),
        "payment_type": payload.payment_type,
        "installment_amount": float(payload.installment_amount),
        "installment_count": int(payload.installment_count),
        "phone_number": payload.phone_number.strip(),
        "image_url": payload.image_url,
        "aadhar_number": payload.aadhar_number.strip(),
        "aadhar_image_url": payload.aadhar_image_url,
        "interest_type": payload.interest_type,
        "interest_value": float(payload.interest_value or 0),
        "external_customer_id": f"{current_user.id}-{uuid4().hex[:6].upper()}",
        "created_at": now,
        "updated_at": now,
    }
    customer_collection.insert_one(document)
    _create_installments_for_customer(
        current_user.id,
        village_id,
        customer_id,
        payload.payment_type,
        float(payload.installment_amount),
        int(payload.installment_count),
        installment_collection,
    )
    return _serialize_customer(document)


@router.get("/customers/{customer_id}", response_model=CustomerDetailResponse)
def get_customer_detail(
    customer_id: str,
    current_user: UserInDB = Depends(get_current_user),
    customer_collection: Collection = Depends(get_customer_collection),
    installment_collection: Collection = Depends(get_installment_collection),
    collection_record_collection: Collection = Depends(get_collection_record_collection),
) -> CustomerDetailResponse:
    owner_id = _effective_owner_id(current_user)
    customer = customer_collection.find_one({"_id": customer_id, "owner_user_id": owner_id})
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    installments = list(installment_collection.find({"owner_user_id": owner_id, "customer_id": customer_id}).sort("due_date", 1))
    collection_history = list(collection_record_collection.find({"owner_user_id": owner_id, "customer_id": customer_id}).sort("collected_at", -1))
    latest_collection_by_installment: dict[str, dict] = {}
    latest_anchor_collection_by_installment: dict[str, dict] = {}
    for record in collection_history:
        latest_collection_by_installment.setdefault(record["installment_id"], record)
        anchor_installment_id = record.get("batch_anchor_installment_id")
        if anchor_installment_id:
            latest_anchor_collection_by_installment.setdefault(anchor_installment_id, record)

    now = _now_ist()
    paid = sum(1 for item in installments if item["status"] == "paid")
    skipped = sum(1 for item in installments if item["status"] == "skipped")
    left = sum(1 for item in installments if item["status"] in {"pending", "partial"})
    metrics_by_customer = _build_customer_metrics(owner_id, [customer_id], installment_collection, collection_record_collection)
    m = metrics_by_customer.get(customer_id, {})
    overdue_installments = int(m.get("overdue_installments", 0))
    overdue_amount = float(m.get("overdue_amount", 0))

    return CustomerDetailResponse(
        customer=_serialize_customer(_enrich_customer(customer, m)),
        installments_paid=paid,
        installments_left=left,
        installments_skipped=skipped,
        overdue_installments=overdue_installments,
        overdue_amount=overdue_amount,
        calendar=_build_calendar_entries(installments, latest_collection_by_installment, latest_anchor_collection_by_installment, now),
        collection_history=_group_collection_history(collection_history),
    )


@router.put("/customers/{customer_id}", response_model=CustomerPublic)
def update_customer(
    customer_id: str,
    payload: CustomerUpdate,
    current_user: UserInDB = Depends(get_current_user),
    customer_collection: Collection = Depends(get_customer_collection),
    installment_collection: Collection = Depends(get_installment_collection),
) -> CustomerPublic:
    _require_write_access(current_user)
    existing = customer_collection.find_one({"_id": customer_id, "owner_user_id": current_user.id})
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    update_document = {
        "full_name": payload.full_name.strip(),
        "address": payload.address.strip(),
        "amount_lent": float(payload.amount_lent),
        "payment_type": payload.payment_type,
        "installment_amount": float(payload.installment_amount),
        "installment_count": int(payload.installment_count),
        "phone_number": payload.phone_number.strip(),
        "image_url": payload.image_url,
        "aadhar_number": payload.aadhar_number.strip(),
        "aadhar_image_url": payload.aadhar_image_url,
        "interest_type": payload.interest_type,
        "interest_value": float(payload.interest_value or 0),
        "updated_at": _now_ist(),
    }

    customer_collection.update_one({"_id": customer_id, "owner_user_id": current_user.id}, {"$set": update_document})

    if (
        existing["payment_type"] != payload.payment_type
        or float(existing["installment_amount"]) != float(payload.installment_amount)
        or int(existing["installment_count"]) != int(payload.installment_count)
    ):
        installment_collection.delete_many({"owner_user_id": current_user.id, "customer_id": customer_id, "status": {"$in": ["pending", "partial", "skipped"]}})
        _create_installments_for_customer(
            current_user.id,
            existing["village_id"],
            customer_id,
            payload.payment_type,
            float(payload.installment_amount),
            int(payload.installment_count),
            installment_collection,
        )

    document = customer_collection.find_one({"_id": customer_id, "owner_user_id": current_user.id})
    return _serialize_customer(document)


@router.post("/installments/{installment_id}/collect", response_model=CollectionRecordPublic, status_code=status.HTTP_201_CREATED)
def collect_installment_payment(
    installment_id: str,
    payload: CollectionRecordCreate,
    current_user: UserInDB = Depends(get_current_user),
    customer_collection: Collection = Depends(get_customer_collection),
    installment_collection: Collection = Depends(get_installment_collection),
    collection_record_collection: Collection = Depends(get_collection_record_collection),
) -> CollectionRecordPublic:
    owner_id = _effective_owner_id(current_user)
    installment = installment_collection.find_one({"_id": installment_id, "owner_user_id": owner_id})
    if installment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Installment not found")
    relevant_installments = list(
        installment_collection.find(
            {
                "owner_user_id": owner_id,
                "customer_id": installment["customer_id"],
                "due_date": {"$lte": installment["due_date"]},
            }
        ).sort("due_date", 1)
    )
    unpaid_installments = [
        item for item in relevant_installments if max(float(item.get("amount_due", 0)) - float(item.get("amount_paid", 0)), 0) > 0
    ]
    remaining = sum(max(float(item.get("amount_due", 0)) - float(item.get("amount_paid", 0)), 0) for item in unpaid_installments)
    if remaining <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This installment is already fully paid.")
    if payload.amount_paid > remaining:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Only {remaining:.2f} is pending up to this installment.")
    if _is_installment_overdue(installment, _now_ist()) and (
        payload.late_interest_type is None
        or payload.late_interest_value <= 0
        or payload.late_interest_base_amount <= 0
        or payload.late_interest_from is None
        or payload.late_interest_to is None
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Late interest is required for overdue payments.")

    amount_to_allocate = float(payload.amount_paid)
    late_interest_days, late_interest_amount = _manual_late_interest_amount(payload)
    late_interest_collected_amount = (
        float(payload.late_interest_collected_amount)
        if payload.late_interest_collected_amount is not None
        else late_interest_amount
    )
    batch_total_amount = round(float(payload.amount_paid) + late_interest_collected_amount, 2)
    batch_id = f"bat-{uuid4().hex[:12]}"
    covered_installment_ids: list[str] = []
    collected_at = _now_ist()
    selected_record: dict | None = None
    last_record: dict | None = None

    for item in unpaid_installments:
        item_amount_due = float(item.get("amount_due", 0))
        item_already_paid = float(item.get("amount_paid", 0))
        item_remaining = max(item_amount_due - item_already_paid, 0)
        if item_remaining <= 0 or amount_to_allocate <= 0:
            continue

        applied_amount = min(item_remaining, amount_to_allocate)
        updated_amount_paid = item_already_paid + applied_amount
        next_status = "paid" if updated_amount_paid >= item_amount_due else "partial"
        covered_installment_ids.append(str(item["_id"]))
        installment_collection.update_one(
            {"_id": item["_id"], "owner_user_id": owner_id},
            {"$set": {"amount_paid": updated_amount_paid, "status": next_status}},
        )

        is_anchor_installment = str(item["_id"]) == installment_id
        record = {
            "_id": f"col-{uuid4().hex[:12]}",
            "collection_batch_id": batch_id,
            "batch_anchor_installment_id": installment_id,
            "owner_user_id": owner_id,
            "village_id": item["village_id"],
            "customer_id": item["customer_id"],
            "installment_id": str(item["_id"]),
            "amount_paid": applied_amount,
            "batch_total_amount": batch_total_amount,
            "covered_installment_count": 0,
            "covered_installment_ids": [],
            "payment_mode": payload.payment_mode,
            "collected_by_user_id": current_user.id,
            "collected_by_name": current_user.full_name,
            "collected_at": collected_at,
            "status_after_payment": next_status,
            "note": payload.note.strip() if payload.note else None,
            "late_interest_base_amount": float(payload.late_interest_base_amount) if is_anchor_installment else 0,
            "late_interest_type": payload.late_interest_type if is_anchor_installment else None,
            "late_interest_value": float(payload.late_interest_value) if is_anchor_installment else 0,
            "late_interest_from": payload.late_interest_from.isoformat() if is_anchor_installment and payload.late_interest_from else None,
            "late_interest_to": payload.late_interest_to.isoformat() if is_anchor_installment and payload.late_interest_to else None,
            "late_interest_days": late_interest_days if is_anchor_installment else 0,
            "late_interest_amount": late_interest_amount if is_anchor_installment else 0,
            "late_interest_collected_amount": late_interest_collected_amount if is_anchor_installment else 0,
        }
        collection_record_collection.insert_one(record)
        last_record = record
        if str(item["_id"]) == installment_id:
            selected_record = record

        amount_to_allocate -= applied_amount

    for installment_record in collection_record_collection.find({"collection_batch_id": batch_id}):
        collection_record_collection.update_one(
            {"_id": installment_record["_id"]},
            {"$set": {"covered_installment_count": len(covered_installment_ids), "covered_installment_ids": covered_installment_ids}},
        )
        if selected_record and installment_record["_id"] == selected_record["_id"]:
            selected_record["covered_installment_count"] = len(covered_installment_ids)
            selected_record["covered_installment_ids"] = covered_installment_ids
        if last_record and installment_record["_id"] == last_record["_id"]:
            last_record["covered_installment_count"] = len(covered_installment_ids)
            last_record["covered_installment_ids"] = covered_installment_ids

    response_record = selected_record or last_record
    if response_record is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No payment could be allocated.")
    return _serialize_collection_record(response_record)


def _ensure_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _csv_values(raw: str | None) -> list[str]:
    if raw is None:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


_FINANCE_SCOPE_ORDER = ("daily", "weekly", "monthly", "yearly")
_PAYMENT_MODE_ORDER = ("cash", "gpay", "phonepe")


def _distinct_batch_field_values(collection_record_collection: Collection, match_filter: dict, field: str) -> list[str]:
    """One row per collection_batch_id so multi-document batches do not duplicate facet values."""
    pipeline: list[dict] = [
        {"$match": match_filter},
        {"$group": {"_id": "$collection_batch_id", "v": {"$first": f"${field}"}}},
        {"$match": {"v": {"$ne": None}}},
        {"$group": {"_id": None, "vals": {"$addToSet": "$v"}}},
    ]
    rows = list(collection_record_collection.aggregate(pipeline))
    if not rows:
        return []
    raw = rows[0].get("vals") or []
    return [str(v) for v in raw if v is not None]


def _build_collections_report_facets(
    *,
    owner_id: str,
    start_dt: datetime,
    end_dt: datetime,
    all_owner_village_ids: list[str],
    finance_scoped_village_ids: list[str],
    table_village_ids: list[str],
    requested_customer_ids: list[str],
    village_collection: Collection,
    customer_collection: Collection,
    collection_record_collection: Collection,
) -> CollectionsReportFacets:
    window = {"collected_at": {"$gte": start_dt, "$lte": end_dt}}

    if not all_owner_village_ids:
        return CollectionsReportFacets(finance_scopes=[], villages=[], customers=[], payment_modes=[])

    m_scope = {"owner_user_id": owner_id, "village_id": {"$in": all_owner_village_ids}, **window}
    vid_for_scope = _distinct_batch_field_values(collection_record_collection, m_scope, "village_id")
    scope_rank: dict[str, int] = {name: idx for idx, name in enumerate(_FINANCE_SCOPE_ORDER)}
    scope_set: set[str] = set()
    if vid_for_scope:
        for doc in village_collection.find({"_id": {"$in": vid_for_scope}}, {"finance_scope": 1}):
            scope_set.add(_village_finance_scope(doc))
    finance_scopes = sorted(scope_set, key=lambda s: scope_rank.get(s, 99))

    villages_out: list[CollectionsReportFacetVillage] = []
    if finance_scoped_village_ids:
        m_village = {"owner_user_id": owner_id, "village_id": {"$in": finance_scoped_village_ids}, **window}
        vid_v = _distinct_batch_field_values(collection_record_collection, m_village, "village_id")
        if vid_v:
            vdocs = list(village_collection.find({"_id": {"$in": vid_v}}, {"name": 1}))
            for doc in sorted(vdocs, key=lambda d: str(d.get("name", "")).lower()):
                villages_out.append(CollectionsReportFacetVillage(id=str(doc["_id"]), name=str(doc.get("name", ""))))

    customers_out: list[CollectionsReportFacetCustomer] = []
    if table_village_ids:
        m_cust = {"owner_user_id": owner_id, "village_id": {"$in": table_village_ids}, **window}
        cid_list = _distinct_batch_field_values(collection_record_collection, m_cust, "customer_id")
        if cid_list:
            for doc in customer_collection.find({"_id": {"$in": cid_list}, "owner_user_id": owner_id}, {"full_name": 1, "village_id": 1}):
                customers_out.append(
                    CollectionsReportFacetCustomer(
                        id=str(doc["_id"]),
                        full_name=str(doc.get("full_name", "")),
                        village_id=str(doc.get("village_id", "")),
                    )
                )
            customers_out.sort(key=lambda c: c.full_name.lower())

    payment_modes: list[str] = []
    if table_village_ids:
        m_pay: dict = {"owner_user_id": owner_id, "village_id": {"$in": table_village_ids}, **window}
        if requested_customer_ids:
            m_pay["customer_id"] = {"$in": requested_customer_ids}
        raw_modes = _distinct_batch_field_values(collection_record_collection, m_pay, "payment_mode")
        mode_set = {m for m in raw_modes if m in _VALID_PAYMENT_MODES}
        payment_modes = [m for m in _PAYMENT_MODE_ORDER if m in mode_set]

    return CollectionsReportFacets(
        finance_scopes=finance_scopes,  # type: ignore[arg-type]
        villages=villages_out,
        customers=customers_out,
        payment_modes=payment_modes,  # type: ignore[arg-type]
    )


@router.get("/collections/report", response_model=CollectionsReportResponse)
def collections_report(
    finance_scope: str = Query(default="all"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    village_id: str | None = Query(default=None),
    customer_id: str | None = Query(default=None),
    payment_mode: str | None = Query(default=None),
    current_user: UserInDB = Depends(get_current_user),
    village_collection: Collection = Depends(get_village_collection),
    customer_collection: Collection = Depends(get_customer_collection),
    collection_record_collection: Collection = Depends(get_collection_record_collection),
) -> CollectionsReportResponse:
    """Aggregated collections by day (line chart) and per-batch rows with optional filters."""
    owner_id = _effective_owner_id(current_user)
    allowed_scopes = {"all", "daily", "weekly", "monthly", "yearly"}
    requested_scopes = _csv_values(finance_scope) or ["all"]
    if any(scope not in allowed_scopes for scope in requested_scopes):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid finance_scope")

    today = _now_ist().date()
    end_day = date_to or today
    start_day = date_from or (end_day - timedelta(days=29))
    if start_day > end_day:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="date_from must be on or before date_to")

    if "all" in requested_scopes:
        village_query = {"owner_user_id": owner_id}
    else:
        scope_filters: list[dict] = []
        if "weekly" in requested_scopes:
            scope_filters.extend([
                {"finance_scope": "weekly"},
                {"finance_scope": {"$exists": False}},
                {"finance_scope": None},
            ])
        non_weekly_scopes = [scope for scope in requested_scopes if scope != "weekly"]
        if non_weekly_scopes:
            scope_filters.append({"finance_scope": {"$in": non_weekly_scopes}})
        village_query = {"owner_user_id": owner_id, "$or": scope_filters} if scope_filters else {"owner_user_id": owner_id}
    village_ids = [str(document["_id"]) for document in village_collection.find(village_query, {"_id": 1})]
    all_owner_village_ids = [str(document["_id"]) for document in village_collection.find({"owner_user_id": owner_id}, {"_id": 1})]
    empty_facets = CollectionsReportFacets(finance_scopes=[], villages=[], customers=[], payment_modes=[])
    if not village_ids:
        return CollectionsReportResponse(
            total_amount=0.0,
            transaction_count=0,
            series=[],
            transactions=[],
            facets=empty_facets,
        )

    requested_village_ids = _csv_values(village_id)
    if requested_village_ids:
        invalid_village_ids = [vid for vid in requested_village_ids if vid not in village_ids]
        if invalid_village_ids:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Village not found")
        allowed_village_ids = requested_village_ids
    else:
        allowed_village_ids = village_ids

    start_dt = datetime.combine(start_day, time.min, tzinfo=IST)
    end_dt = datetime.combine(end_day, time(23, 59, 59, 999999), tzinfo=IST)

    match_filter: dict = {
        "owner_user_id": owner_id,
        "village_id": {"$in": allowed_village_ids},
        "collected_at": {"$gte": start_dt, "$lte": end_dt},
    }

    requested_customer_ids = _csv_values(customer_id)
    if requested_customer_ids:
        customer_docs_for_filter = list(customer_collection.find({"_id": {"$in": requested_customer_ids}, "owner_user_id": owner_id}))
        valid_customer_ids = {str(customer["_id"]) for customer in customer_docs_for_filter if str(customer["village_id"]) in allowed_village_ids}
        if valid_customer_ids != set(requested_customer_ids):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        match_filter["customer_id"] = {"$in": requested_customer_ids}

    requested_payment_modes = _csv_values(payment_mode)
    if requested_payment_modes:
        if any(mode not in _VALID_PAYMENT_MODES for mode in requested_payment_modes):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payment_mode")
        match_filter["payment_mode"] = {"$in": requested_payment_modes}

    pipeline = [
        {"$match": match_filter},
        {
            "$group": {
                "_id": "$collection_batch_id",
                "collected_at": {"$max": "$collected_at"},
                "amount_paid": {"$sum": "$amount_paid"},
                "late_interest_amount": {"$max": "$late_interest_amount"},
                "late_interest_collected_amount": {"$max": "$late_interest_collected_amount"},
                "village_id": {"$first": "$village_id"},
                "customer_id": {"$first": "$customer_id"},
                "payment_mode": {"$first": "$payment_mode"},
                "collected_by_name": {"$first": "$collected_by_name"},
                "note": {"$first": "$note"},
            }
        },
        {"$sort": {"collected_at": -1}},
        {"$limit": 500},
    ]
    grouped = list(collection_record_collection.aggregate(pipeline))

    by_day: dict[str, float] = {}
    by_day_count: dict[str, int] = {}
    total_amount = 0.0

    customer_ids = sorted({str(row["customer_id"]) for row in grouped})
    village_id_set = sorted({str(row["village_id"]) for row in grouped})

    customer_docs = (
        list(customer_collection.find({"_id": {"$in": customer_ids}}, {"full_name": 1})) if customer_ids else []
    )
    village_docs = (
        list(village_collection.find({"_id": {"$in": village_id_set}}, {"name": 1, "day": 1, "finance_scope": 1})) if village_id_set else []
    )

    customer_names = {str(doc["_id"]): str(doc.get("full_name", "")) for doc in customer_docs}
    village_names = {str(doc["_id"]): str(doc.get("name", "")) for doc in village_docs}
    village_days = {str(doc["_id"]): str(doc.get("day", "")) for doc in village_docs}
    village_scopes = {str(doc["_id"]): _village_finance_scope(doc) for doc in village_docs}

    transactions: list[CollectionTransactionRow] = []
    for row in grouped:
        cat_raw = row["collected_at"]
        cat = _ensure_ist(cat_raw) if isinstance(cat_raw, datetime) else _now_ist()
        day_key = cat.date().isoformat()

        amt = float(row["amount_paid"]) + float(
            row.get("late_interest_collected_amount", row.get("late_interest_amount", 0)) or 0
        )
        total_amount += amt
        by_day[day_key] = by_day.get(day_key, 0.0) + amt
        by_day_count[day_key] = by_day_count.get(day_key, 0) + 1

        cid = str(row["customer_id"])
        vid = str(row["village_id"])
        mode = row["payment_mode"]
        if mode not in _VALID_PAYMENT_MODES:
            mode = "cash"

        transactions.append(
            CollectionTransactionRow(
                id=str(row["_id"]),
                collected_at=cat,
                amount_paid=amt,
                payment_mode=mode,  # type: ignore[arg-type]
                collected_by_name=str(row.get("collected_by_name", "")),
                customer_id=cid,
                customer_full_name=customer_names.get(cid, cid),
                village_id=vid,
                village_name=village_names.get(vid, vid),
                finance_scope=village_scopes.get(vid, "weekly"),  # type: ignore[arg-type]
                village_day=village_days.get(vid),
                note=row.get("note"),
            )
        )

    series: list[CollectionTimeseriesPoint] = []
    cursor_day = start_day
    while cursor_day <= end_day:
        key = cursor_day.isoformat()
        series.append(
            CollectionTimeseriesPoint(
                date=cursor_day,
                amount=float(by_day.get(key, 0.0)),
                count=int(by_day_count.get(key, 0)),
            )
        )
        cursor_day += timedelta(days=1)

    facets = _build_collections_report_facets(
        owner_id=owner_id,
        start_dt=start_dt,
        end_dt=end_dt,
        all_owner_village_ids=all_owner_village_ids,
        finance_scoped_village_ids=village_ids,
        table_village_ids=allowed_village_ids,
        requested_customer_ids=requested_customer_ids,
        village_collection=village_collection,
        customer_collection=customer_collection,
        collection_record_collection=collection_record_collection,
    )

    return CollectionsReportResponse(
        total_amount=total_amount,
        transaction_count=len(transactions),
        series=series,
        transactions=transactions,
        facets=facets,
    )


@router.delete("/customers/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer(
    customer_id: str,
    current_user: UserInDB = Depends(get_current_user),
    customer_collection: Collection = Depends(get_customer_collection),
    installment_collection: Collection = Depends(get_installment_collection),
) -> None:
    _require_write_access(current_user)
    installment_collection.delete_many({"owner_user_id": current_user.id, "customer_id": customer_id})
    result = customer_collection.delete_one({"_id": customer_id, "owner_user_id": current_user.id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")