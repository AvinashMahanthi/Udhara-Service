from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PaymentType = Literal["daily", "weekly", "monthly", "yearly"]
FinanceScope = Literal["daily", "weekly", "monthly", "yearly"]
InstallmentStatus = Literal["paid", "partial", "skipped", "pending"]
PaymentMode = Literal["phonepe", "gpay", "cash"]
InterestType = Literal["percentage", "rupees_100", "rupees", "daily_rupees"]


class VillageCreate(BaseModel):
    name: str = Field(min_length=2)
    day: str = Field(min_length=3)
    finance_scope: FinanceScope = "weekly"


class VillageUpdate(BaseModel):
    name: str = Field(min_length=2)
    day: str = Field(min_length=3)


class VillagePublic(BaseModel):
    id: str
    owner_user_id: str
    name: str
    day: str
    finance_scope: FinanceScope = "weekly"
    customer_count: int = 0


class CustomerCreate(BaseModel):
    full_name: str = Field(min_length=2)
    address: str = Field(min_length=5)
    amount_lent: float = Field(gt=0)
    payment_type: PaymentType
    installment_amount: float = Field(gt=0)
    installment_count: int = Field(gt=0)
    phone_number: str = Field(pattern=r"^\d{10}$")
    image_url: str | None = None
    aadhar_number: str = Field(pattern=r"^\d{12}$")
    aadhar_image_url: str | None = None
    interest_type: InterestType | None = None
    interest_value: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_loan_amount_vs_installments(self) -> "CustomerCreate":
        total_installments = float(self.installment_amount) * int(self.installment_count)
        if total_installments < float(self.amount_lent):
            raise ValueError("Total installments must be greater than or equal to loan amount.")
        if self.interest_value > 0 and self.interest_type is None:
            raise ValueError("Interest type is required when interest value is greater than 0.")
        return self


class CustomerUpdate(CustomerCreate):
    pass


class CustomerPublic(BaseModel):
    id: str
    owner_user_id: str
    village_id: str
    full_name: str
    address: str
    amount_lent: float
    payment_type: PaymentType
    installment_amount: float
    installment_count: int
    phone_number: str
    image_url: str | None = None
    aadhar_number: str
    aadhar_image_url: str | None = None
    external_customer_id: str
    interest_type: InterestType | None = None
    interest_value: float = 0
    overdue_installments: int = 0
    overdue_amount: float = 0
    due_this_month_installments: int = 0
    due_this_month_amount: float = 0
    due_this_year_installments: int = 0
    due_this_year_amount: float = 0
    due_today_installments: int = 0
    due_today_amount: float = 0
    total_collected: float = 0
    last_collected_at: datetime | None = None
    last_collected_by_name: str | None = None


class CollectionRecordCreate(BaseModel):
    amount_paid: float = Field(gt=0)
    payment_mode: PaymentMode
    note: str | None = Field(default=None, max_length=240)
    late_interest_base_amount: float = Field(default=0, ge=0)
    late_interest_type: InterestType | None = None
    late_interest_value: float = Field(default=0, ge=0)
    late_interest_from: date | None = None
    late_interest_to: date | None = None
    late_interest_collected_amount: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_late_interest(self) -> "CollectionRecordCreate":
        has_interest_input = (
            self.late_interest_base_amount > 0
            or self.late_interest_value > 0
            or self.late_interest_from is not None
            or self.late_interest_to is not None
        )
        if has_interest_input:
            if self.late_interest_type is None:
                raise ValueError("Late interest type is required.")
            if self.late_interest_value <= 0:
                raise ValueError("Late interest value must be greater than 0.")
            if self.late_interest_base_amount <= 0:
                raise ValueError("Late interest base amount must be greater than 0.")
            if self.late_interest_from is None or self.late_interest_to is None:
                raise ValueError("Late interest from and to dates are required.")
            if self.late_interest_from > self.late_interest_to:
                raise ValueError("Late interest from date must be on or before to date.")
        return self


class CollectionRecordPublic(BaseModel):
    id: str
    collection_batch_id: str
    batch_anchor_installment_id: str
    owner_user_id: str
    village_id: str
    customer_id: str
    installment_id: str
    amount_paid: float
    covered_installment_count: int = 1
    covered_installment_ids: list[str] = []
    payment_mode: PaymentMode
    collected_by_user_id: str
    collected_by_name: str
    collected_at: datetime
    status_after_payment: InstallmentStatus
    note: str | None = None
    late_interest_base_amount: float = 0
    late_interest_type: InterestType | None = None
    late_interest_value: float = 0
    late_interest_from: date | None = None
    late_interest_to: date | None = None
    late_interest_days: int = 0
    late_interest_amount: float = 0
    late_interest_collected_amount: float = 0


class DelayedCustomerPublic(BaseModel):
    customer_id: str
    full_name: str
    village_name: str
    phone_number: str
    overdue_installments: int
    overdue_amount: float
    last_collected_at: datetime | None = None
    last_collected_by_name: str | None = None


class InstallmentCalendarEntry(BaseModel):
    installment_id: str
    due_date: date
    amount_due: float
    amount_paid: float
    amount_remaining: float
    status: InstallmentStatus
    is_overdue: bool = False
    last_payment_mode: PaymentMode | None = None
    last_collected_at: datetime | None = None
    last_collected_by_name: str | None = None
    latest_payment_event_amount: float | None = None
    latest_payment_cover_count: int | None = None
    principal_due: float | None = None
    interest_added: float = 0
    last_interest_applied_date: date | None = None


class CustomerDetailResponse(BaseModel):
    customer: CustomerPublic
    installments_paid: int
    installments_left: int
    installments_skipped: int
    overdue_installments: int
    overdue_amount: float
    calendar: list[InstallmentCalendarEntry]
    collection_history: list[CollectionRecordPublic]


class CollectionTimeseriesPoint(BaseModel):
    """One calendar day of aggregated collections (sum of payment batches)."""

    date: date
    amount: float
    count: int = 0


class CollectionTransactionRow(BaseModel):
    """One logical collection (batch) for reporting tables."""

    id: str
    collected_at: datetime
    amount_paid: float
    payment_mode: PaymentMode
    collected_by_name: str
    customer_id: str
    customer_full_name: str
    village_id: str
    village_name: str
    finance_scope: FinanceScope = "weekly"
    village_day: str | None = None
    note: str | None = None


class CollectionsReportFacetVillage(BaseModel):
    id: str
    name: str


class CollectionsReportFacetCustomer(BaseModel):
    id: str
    full_name: str
    village_id: str


class CollectionsReportFacets(BaseModel):
    """Filter choices derived from collection activity in the selected date range."""

    finance_scopes: list[FinanceScope]
    villages: list[CollectionsReportFacetVillage]
    customers: list[CollectionsReportFacetCustomer]
    payment_modes: list[PaymentMode]


class CollectionsReportResponse(BaseModel):
    total_amount: float
    transaction_count: int
    series: list[CollectionTimeseriesPoint]
    transactions: list[CollectionTransactionRow]
    facets: CollectionsReportFacets


class InstallmentInDB(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="_id")
    owner_user_id: str
    customer_id: str
    village_id: str
    due_date: datetime
    amount_due: float
    amount_paid: float = 0
    status: InstallmentStatus = "pending"
    principal_due: float | None = None
    interest_added: float = 0
    last_interest_applied_date: date | None = None


class CollectionRecordInDB(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="_id")
    collection_batch_id: str
    batch_anchor_installment_id: str
    owner_user_id: str
    village_id: str
    customer_id: str
    installment_id: str
    amount_paid: float
    covered_installment_count: int = 1
    covered_installment_ids: list[str] = []
    payment_mode: PaymentMode
    collected_by_user_id: str
    collected_by_name: str
    collected_at: datetime
    status_after_payment: InstallmentStatus
    note: str | None = None


class VillageInDB(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="_id")
    owner_user_id: str
    name: str
    day: str
    created_at: datetime
    updated_at: datetime


class CustomerInDB(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="_id")
    owner_user_id: str
    village_id: str
    full_name: str
    address: str
    amount_lent: float
    payment_type: PaymentType
    installment_amount: float
    installment_count: int
    phone_number: str
    image_url: str | None = None
    aadhar_number: str
    aadhar_image_url: str | None = None
    interest_type: InterestType | None = None
    interest_value: float = 0
    external_customer_id: str
    created_at: datetime
    updated_at: datetime

