from pydantic import AliasChoices
from datetime import datetime, timezone
from typing import Annotated, List, Optional
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

PyObjectId = Annotated[str, BeforeValidator(str)]


class Verification(BaseModel):
    """Replication-fidelity state for a record, attached by the sync service.

    Mirrors the payload returned by
    `services.verification_service.get_verification`.
    """

    status: str = "PENDING"
    verified: bool = False
    last_verified_at: Optional[datetime] = None
    main_version: Optional[int] = None
    secondary_version: Optional[int] = None
    error: Optional[str] = None


# --- Gate Pass / Receiving ---
class GatePassItem(BaseModel):
    item_name: str
    category: Optional[str] = None
    specification: Optional[str] = None
    client_qty: int = Field(ge=0)
    received_qty: int = Field(ge=0)
    difference: int = 0
    mismatch_reason: Optional[str] = None  # MISSING, EXTRA, COUNTING_ERROR, DAMAGED, OTHER
    mismatch_notes: Optional[str] = None
    rewashed: bool = Field(default=False, description="Tagged as a free re-wash; never billed")


class GatePassCreate(BaseModel):
    gate_pass_number: str
    client_name: str
    receiving_date: datetime
    received_by: str
    items: List[GatePassItem] = Field(min_length=1)
    notes: Optional[str] = None
    quotation_id: Optional[str] = None


class GatePassAdjustment(BaseModel):
    """Legacy quick-adjust body (POST /gatepasses/{id}/adjust).

    Quantities are never mutated inline anymore — the endpoint stages a
    controlled adjustment request. ``specification`` is optional so a request
    always targets the correct variant of an item on the gate pass.
    """

    item_name: str
    specification: Optional[str] = None
    corrected_qty: int = Field(ge=0)
    reason: str = Field(min_length=1, description="Reason is mandatory")


class GatePassDateUpdate(BaseModel):
    receiving_date: datetime
    reason: Optional[str] = None


class GatePassUpdate(BaseModel):
    """Full edit of a gate pass that is not yet fully delivered."""

    client_name: Optional[str] = None
    received_by: Optional[str] = None
    notes: Optional[str] = None
    items: Optional[List[GatePassItem]] = Field(default=None, min_length=1)


class GatePassMarkDelivered(BaseModel):
    """DEPRECATED — legacy note-based closure endpoint.

    Kept only so old clients get a clear 400 directing them to the
    quantity-based catch-up delivery flow. A note can never close a gate
    pass anymore (see ``GatePassCatchUpDelivery``).
    """

    note: str = Field(min_length=1, description="Required note explaining the delivery")
    delivered_date: Optional[datetime] = None


class CatchUpDeliveryItem(BaseModel):
    item_name: str
    specification: Optional[str] = None
    quantity: int = Field(gt=0)


class GatePassCatchUpDelivery(BaseModel):
    """Quantity-based catch-up delivery.

    Replaces the old note-only mark-delivered. Creates a REAL delivery
    record with explicit item quantities plus the explanatory note.
    """

    note: str = Field(min_length=1, description="Required note explaining the catch-up")
    delivered_date: Optional[datetime] = None
    items: List[CatchUpDeliveryItem] = Field(min_length=1)


class AdjustmentItem(BaseModel):
    item_name: str
    specification: Optional[str] = None


class GatePassAdjustmentRequest(BaseModel):
    """Controlled adjustment request. Approved adjustments modify balances;
    the original recorded events are preserved in the journal."""

    gate_pass_id: str
    item_name: str
    specification: Optional[str] = None
    corrected_qty: int = Field(ge=0)
    reason: str = Field(min_length=1, description="Reason is mandatory")


class GatePassModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )

    verification: Optional[Verification] = None
    gate_pass_number: str
    client_name: str
    receiving_date: datetime
    received_by: str
    items: List[GatePassItem]
    status: str  # RECEIVED, PROCESSING, READY_FOR_DELIVERY, PARTIALLY_DELIVERED, DELIVERED, CANCELLED
    notes: Optional[str] = None
    quotation_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    adjustments: Optional[List[dict]] = []
    marked_delivered: Optional[dict] = None


# --- Delivery ---
class DeliveryItem(BaseModel):
    item_name: str
    specification: Optional[str] = None
    quantity: int = Field(gt=0)


class DeliveryCreate(BaseModel):
    gate_pass_id: str
    client_name: str
    delivery_date: datetime
    delivered_by: str
    received_by: str  # Customer representative signature name
    items: List[DeliveryItem] = Field(min_length=1)
    notes: Optional[str] = None

class DeliveryDateUpdate(BaseModel):
    """Special-case correction of a recorded delivery's dispatch date."""

    delivery_date: datetime
    reason: Optional[str] = None


# --- Balance adjustments (quantity corrections on a delivery) ---
class BalanceAdjustmentCreate(BaseModel):
    """A signed correction posted when a recorded delivery was wrong.

    ``quantity`` is signed and must be non-zero:
      positive -> we under-delivered / lost / damaged pieces, so the client is
                  owed that many more (the balance goes UP)
      negative -> we recorded more as sent than the client actually took, so
                  fewer pieces are outstanding (the balance goes DOWN)

    A ``reason`` is mandatory: these are corrections to the client's balance,
    so every one is auditable. The correction is a PIECE COUNT only — billing
    still derives from the gate pass received quantity, so no invoice moves.
    """

    item_name: str = Field(min_length=1)
    specification: Optional[str] = None
    quantity: int = Field(description="Signed correction; must not be zero")
    reason: str = Field(min_length=1, description="Reason is mandatory")
    notes: Optional[str] = None
    gate_pass_id: str = Field(min_length=1)
    # Attach to a delivery so the correction is shown on that delivery note.
    # When omitted it still applies to the gate pass as a whole.
    delivery_id: Optional[str] = None

    @field_validator("quantity")
    @classmethod
    def _non_zero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("quantity must not be zero; omit the adjustment instead")
        return v


class BalanceAdjustmentModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )
    gate_pass_id: str
    delivery_id: Optional[str] = None
    item_name: str
    specification: str = ""
    quantity: int
    reason: str
    notes: Optional[str] = None
    status: str = "POSTED"  # POSTED | VOID
    created_by: str = ""
    created_by_id: str = ""
    created_at: datetime
    voided_at: Optional[datetime] = None
    voided_by: Optional[str] = None
    void_reason: Optional[str] = None


class DeliveryBalanceItem(BaseModel):
    item_name: str
    specification: str = ""
    category: str = ""
    previous_balance_qty: int
    received_qty: int
    delivered_qty: int
    balance_adjustment_qty: int
    current_balance_qty: int
    reconciles: bool
    flags: List[str] = []


class DeliveryBalanceReport(BaseModel):
    """The four running-balance figures shown on the printed delivery note."""

    delivery_id: str
    gate_pass_id: str
    gate_pass_number: Optional[str] = None
    client_name: Optional[str] = None
    delivery_date: Optional[datetime] = None
    items: List[DeliveryBalanceItem]
    totals: dict
    flags: List[str] = []


class DeliveryModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )

    verification: Optional[Verification] = None
    gate_pass_id: str
    client_name: str
    delivery_date: datetime
    delivered_by: str
    received_by: str
    items: List[DeliveryItem]
    status: str  # DELIVERED, CANCELLED
    notes: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


# --- Dispatch (pickup / delivery scheduling) ---
class DispatchCreate(BaseModel):
    job_type: str = "delivery"  # "pickup" | "delivery"
    order_id: Optional[str] = None  # linked quotation/order id
    client_name: str
    address: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    assigned_to: Optional[str] = None  # driver / field staff
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    notes: Optional[str] = None


class DispatchUpdate(BaseModel):
    status: Optional[str] = None  # SCHEDULED|ASSIGNED|EN_ROUTE|COMPLETED|CANCELLED
    assigned_to: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    notes: Optional[str] = None


class DispatchOptimize(BaseModel):
    assigned_to: str
    date: Optional[str] = None  # "YYYY-MM-DD"; optimize that day's jobs


class DispatchModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )

    verification: Optional[Verification] = None
    job_type: str
    order_id: Optional[str] = None
    client_name: str
    address: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    status: str  # SCHEDULED|ASSIGNED|EN_ROUTE|COMPLETED|CANCELLED
    assigned_to: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# --- Billing ---
class BillItemIn(BaseModel):
    item_name: str
    category: Optional[str] = None
    unit_price: float = Field(ge=0)
    quantity: int = Field(gt=0)


class BillItemOut(BaseModel):
    item_name: str
    category: Optional[str] = None
    unit_price: float
    quantity: int
    line_total: float


class BillCreate(BaseModel):
    quotation_id: Optional[str] = None
    client_name: str
    quotation_title: Optional[str] = None
    items: Optional[List[BillItemIn]] = None
    delivery_ids: Optional[List[str]] = None
    gate_pass_id: Optional[str] = None
    instant: bool = False
    notes: Optional[str] = None
    discounts: Optional[float] = 0.0
    transport_fee: Optional[float] = 0.0
    taxes: Optional[float] = 0.0
    additional_charges: Optional[float] = 0.0


class BillModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )

    verification: Optional[Verification] = None
    quotation_id: str
    client_name: str
    quotation_title: Optional[str] = None
    items: List[BillItemOut]
    total_quantity: int
    total_amount: float  # Base amount before adjustments
    discounts: float = 0.0
    transport_fee: float = 0.0
    taxes: float = 0.0
    additional_charges: float = 0.0
    grand_total: float
    payment_status: str  # DRAFT, PENDING, ISSUED, PARTIALLY_PAID, PAID, CANCELLED
    paid_amount: float = 0.0
    outstanding_amount: float
    delivery_ids: Optional[List[str]] = []
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class BillListResponse(BaseModel):
    items: List[BillModel]
    total: int


# --- Payments ---
class PaymentCreate(BaseModel):
    amount: float = Field(gt=0)
    payment_method: str  # CASH, CARD, BANK_TRANSFER, CHEQUE
    payment_date: datetime
    reference: Optional[str] = None
    notes: Optional[str] = None


# --- Loyalty ---
def loyalty_tier(points: int) -> str:
    if points >= 2000:
        return "PLATINUM"
    if points >= 500:
        return "GOLD"
    if points >= 100:
        return "SILVER"
    return "BRONZE"


class LoyaltyAdjust(BaseModel):
    client_name: str
    delta_points: int
    reason: Optional[str] = None


class LoyaltyAccount(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )
    client_name: str
    points: int
    tier: str
    visits: int
    created_at: datetime
    updated_at: datetime


class PaymentModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )

    verification: Optional[Verification] = None
    bill_id: str
    client_name: str
    amount: float
    payment_method: str
    payment_date: datetime
    reference: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime


# --- Linen Tracking ---
LINEN_STATUSES = [
    "IN_STOCK", "AT_CLIENT", "COLLECTED", "AT_LAUNDRY",
    "WASHING", "DRYING", "PRESSING", "READY",
    "DELIVERED", "MISSING", "DAMAGED", "RETIRED",
]

LINEN_CATEGORIES = [
    "BEDSHEET", "PILLOWCASE", "TOWEL", "DUVET_COVER",
    "BATH_MAT", "UNIFORM", "TABLECLOTH", "NAPKIN",
    "ROBE", "SLIPPER", "OTHER",
]


class LinenCreate(BaseModel):
    category: str
    item_type: str
    description: Optional[str] = None
    size: Optional[str] = None
    color: Optional[str] = None
    client_name: str
    department: Optional[str] = None
    notes: Optional[str] = None


class LinenBulkCreate(BaseModel):
    category: str
    item_type: str
    description: Optional[str] = None
    size: Optional[str] = None
    color: Optional[str] = None
    client_name: str
    department: Optional[str] = None
    quantity: int = Field(gt=0, le=10000)
    notes: Optional[str] = None


class LinenUpdate(BaseModel):
    category: Optional[str] = None
    item_type: Optional[str] = None
    description: Optional[str] = None
    size: Optional[str] = None
    color: Optional[str] = None
    client_name: Optional[str] = None
    department: Optional[str] = None
    status: Optional[str] = None
    condition: Optional[str] = None
    location: Optional[str] = None
    notes: Optional[str] = None


class LinenScanAction(BaseModel):
    action: str  # collect, receive, start_wash, complete_wash, press, ready, deliver, mark_missing, mark_damaged, retire
    location: Optional[str] = None
    user: Optional[str] = None
    related_order: Optional[str] = None
    notes: Optional[str] = None


class LinenEventModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)
    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )
    linen_id: str
    action: str
    from_status: Optional[str] = None
    to_status: str
    location: Optional[str] = None
    user: Optional[str] = None
    related_order: Optional[str] = None
    notes: Optional[str] = None
    timestamp: datetime


class LinenModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)
    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )
    linen_id: str
    category: str
    item_type: str
    description: Optional[str] = None
    size: Optional[str] = None
    color: Optional[str] = None
    client_name: str
    department: Optional[str] = None
    status: str  # IN_STOCK, AT_CLIENT, COLLECTED, AT_LAUNDRY, WASHING, DRYING, PRESSING, READY, DELIVERED, MISSING, DAMAGED, RETIRED
    condition: str  # NEW, GOOD, FAIR, WORN, DAMAGED
    location: Optional[str] = None
    wash_count: int = 0
    last_washed_date: Optional[datetime] = None
    last_scanned_date: Optional[datetime] = None
    retirement_date: Optional[datetime] = None
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class LinenListResponse(BaseModel):
    items: List[LinenModel]
    total: int


class LinenEventListResponse(BaseModel):
    items: List[LinenEventModel]
    total: int


class LinenStats(BaseModel):
    total: int
    in_stock: int = 0
    at_client: int = 0
    collected: int = 0
    at_laundry: int = 0
    washing: int = 0
    drying: int = 0
    pressing: int = 0
    ready: int = 0
    delivered: int = 0
    missing: int = 0
    damaged: int = 0
    retired: int = 0
    total_wash_cycles: int = 0
    recently_scanned: int = 0


class LinenTagGenerate(BaseModel):
    category: str
    item_type: str
    client_name: str
    quantity: int = Field(gt=0, le=10000)
    size: Optional[str] = None
    color: Optional[str] = None
    department: Optional[str] = None


class LinenListParams(BaseModel):
    search: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    client_name: Optional[str] = None
    condition: Optional[str] = None
    sort_by: str = "created_at"
    sort_order: str = "desc"
    skip: int = 0
    limit: int = 50


# --- Garment Returns ---

RETURN_REASONS = ["WRONG_ITEM", "DAMAGED", "MISSING", "OTHER"]
RETURN_CONDITIONS = ["GOOD", "DAMAGED", "STAINED", "LOST"]
RETURN_ACTIONS = ["RECEIVE_BACK", "RE_WASH", "DISCARD", "COMPENSATE"]
RETURN_ADJUSTMENT_TYPES = ["NONE", "QUANTITY_REDUCE", "AMOUNT_REDUCE", "COMPENSATE"]
RETURN_STATUSES = ["PENDING", "RECEIVED", "PROCESSED"]


class ReturnItem(BaseModel):
    item_name: str
    specification: Optional[str] = None
    returned_qty: int = Field(gt=0)
    reason: str  # WRONG_ITEM, DAMAGED, MISSING, OTHER
    condition: str = "GOOD"  # GOOD, DAMAGED, STAINED, LOST
    action: str = "RECEIVE_BACK"  # RECEIVE_BACK, RE_WASH, DISCARD, COMPENSATE
    notes: Optional[str] = None
    resend_status: Optional[str] = None  # PENDING, SENT (for RECEIVE_BACK/RE_WASH items)
    resent_at: Optional[datetime] = None


class BillAdjustment(BaseModel):
    adjustment_type: str = "NONE"  # NONE, QUANTITY_REDUCE, AMOUNT_REDUCE, COMPENSATE
    amount: float = 0.0
    notes: Optional[str] = None


class ReturnCreate(BaseModel):
    gate_pass_id: str
    delivery_id: Optional[str] = None
    client_name: str
    items: List[ReturnItem] = Field(min_length=1)
    bill_adjustment: Optional[BillAdjustment] = None
    notes: Optional[str] = None


class ReturnUpdate(BaseModel):
    status: Optional[str] = None
    items: Optional[List[ReturnItem]] = None
    bill_adjustment: Optional[BillAdjustment] = None
    notes: Optional[str] = None


class ReturnModel(BaseModel):
    id: Optional[PyObjectId] = Field(alias="_id", default=None)
    return_id: str
    gate_pass_id: str
    delivery_id: Optional[str] = None
    client_name: str
    client_name_search: Optional[str] = None
    items: List[ReturnItem]
    bill_adjustment: Optional[BillAdjustment] = None
    status: str = "PENDING"
    recorded_by: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    model_config = ConfigDict(populate_by_name=True)


# --- Shop Bills ---

SHOP_BILL_STATUSES = ["PENDING", "PROCESSING", "DELIVERED", "COMPLETED", "CANCELLED"]
SHOP_PAYMENT_STATUSES = ["DRAFT", "PENDING", "PARTIALLY_PAID", "PAID", "CANCELLED"]


class ShopBillItem(BaseModel):
    item_name: str
    specification: Optional[str] = None
    category: Optional[str] = None
    unit_price: float = Field(ge=0)
    quantity: int = Field(gt=0)
    discount: float = 0.0
    discount_type: str = "FIXED"  # FIXED or PERCENT
    line_total: float = 0.0


class ShopBillCreate(BaseModel):
    bill_number: Optional[str] = None  # auto-generated if not provided
    client_name: str
    quotation_id: Optional[str] = None
    items: List[ShopBillItem] = Field(min_length=1)
    notes: Optional[str] = None
    delivery_date: Optional[datetime] = None
    discounts: Optional[float] = 0.0
    transport_fee: Optional[float] = 0.0
    taxes: Optional[float] = 0.0
    tags: Optional[List[str]] = []
    is_recurring: Optional[bool] = False
    recurring_interval: Optional[str] = None  # DAILY, WEEKLY, BIWEEKLY, MONTHLY
    recurring_end_date: Optional[datetime] = None
    locked: Optional[bool] = False


class ShopBillUpdate(BaseModel):
    status: Optional[str] = None
    payment_status: Optional[str] = None
    notes: Optional[str] = None
    delivery_date: Optional[datetime] = None
    items: Optional[List[ShopBillItem]] = None
    discounts: Optional[float] = None
    transport_fee: Optional[float] = None
    taxes: Optional[float] = None
    tags: Optional[List[str]] = None
    locked: Optional[bool] = None


class ShopBillPayment(BaseModel):
    amount: float = Field(gt=0)
    payment_method: str  # CASH, CARD, BANK_TRANSFER, CHEQUE
    payment_date: datetime
    reference: Optional[str] = None
    notes: Optional[str] = None


class LegacyInvoiceEntry(BaseModel):
    """One old paper bill aggregated into a legacy invoice."""
    date: Optional[str] = None  # ISO date (YYYY-MM-DD) as entered
    bill_number: Optional[str] = None
    amount: float = Field(default=0, ge=0)


class LegacyInvoiceCreate(BaseModel):
    """Request to persist a manual legacy invoice."""
    shop_name: str
    description: Optional[str] = None
    entries: List[LegacyInvoiceEntry] = Field(min_length=1)


class ShopBillModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )
    bill_number: str
    client_name: str
    quotation_id: Optional[str] = None
    items: List[ShopBillItem]
    total_quantity: int
    total_amount: float
    discounts: float = 0.0
    transport_fee: float = 0.0
    taxes: float = 0.0
    grand_total: float
    status: str  # PENDING, PROCESSING, DELIVERED, COMPLETED, CANCELLED
    payment_status: str  # DRAFT, PENDING, PARTIALLY_PAID, PAID, CANCELLED
    paid_amount: float = 0.0
    outstanding_amount: float
    notes: Optional[str] = None
    notes_history: Optional[List[dict]] = []
    delivery_date: Optional[datetime] = None
    tags: Optional[List[str]] = []
    locked: bool = False
    is_recurring: bool = False
    recurring_interval: Optional[str] = None
    recurring_end_date: Optional[datetime] = None
    parent_bill_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ShopBillListResponse(BaseModel):
    items: List[ShopBillModel]
    total: int


class ShopBillBulkStatus(BaseModel):
    bill_ids: List[str] = Field(min_length=1)
    status: str


class ShopBillSplit(BaseModel):
    item_indices: List[int]  # indices of items to move to new bill


class ShopBillMerge(BaseModel):
    bill_ids: List[str]  # IDs of bills to merge (first bill becomes base)


class BillTemplateCreate(BaseModel):
    name: str
    client_name: Optional[str] = None
    items: List[ShopBillItem] = Field(min_length=1)
    discounts: float = 0.0
    transport_fee: float = 0.0
    taxes: float = 0.0
    notes: Optional[str] = None


class BillTemplateModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )
    name: str
    client_name: Optional[str] = None
    items: List[ShopBillItem]
    discounts: float = 0.0
    transport_fee: float = 0.0
    taxes: float = 0.0
    notes: Optional[str] = None
    use_count: int = 0
    created_at: datetime
    updated_at: datetime