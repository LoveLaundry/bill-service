from pydantic import AliasChoices
from datetime import datetime
from typing import Annotated, List, Optional
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

PyObjectId = Annotated[str, BeforeValidator(str)]


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


class GatePassCreate(BaseModel):
    gate_pass_number: str
    client_name: str
    receiving_date: datetime
    received_by: str
    items: List[GatePassItem] = Field(min_length=1)
    notes: Optional[str] = None
    quotation_id: Optional[str] = None


class GatePassAdjustment(BaseModel):
    item_name: str
    corrected_qty: int = Field(ge=0)
    reason: str


class GatePassDateUpdate(BaseModel):
    receiving_date: datetime
    reason: Optional[str] = None


class GatePassUpdate(BaseModel):
    """Full edit of a gate pass that is not yet fully delivered."""

    client_name: Optional[str] = None
    received_by: Optional[str] = None
    notes: Optional[str] = None
    items: Optional[List[GatePassItem]] = Field(default=None, min_length=1)


class GatePassModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )
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


class DeliveryModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(
        validation_alias=AliasChoices("_id", "id"),
        serialization_alias="id",
    )
    gate_pass_id: str
    client_name: str
    delivery_date: datetime
    delivered_by: str
    received_by: str
    items: List[DeliveryItem]
    status: str  # DELIVERED, CANCELLED
    notes: Optional[str] = None
    created_at: datetime


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