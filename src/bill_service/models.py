from datetime import datetime
from typing import Annotated, List, Optional
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

PyObjectId = Annotated[str, BeforeValidator(str)]


# --- Gate Pass / Receiving ---
class GatePassItem(BaseModel):
    item_name: str
    category: Optional[str] = None
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


class GatePassModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
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

    id: PyObjectId = Field(alias="_id")
    gate_pass_id: str
    client_name: str
    delivery_date: datetime
    delivered_by: str
    received_by: str
    items: List[DeliveryItem]
    status: str  # DELIVERED, CANCELLED
    notes: Optional[str] = None
    created_at: datetime


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
    quotation_id: str
    client_name: str
    quotation_title: Optional[str] = None
    items: Optional[List[BillItemIn]] = None
    delivery_ids: Optional[List[str]] = None
    notes: Optional[str] = None
    discounts: Optional[float] = 0.0
    transport_fee: Optional[float] = 0.0
    taxes: Optional[float] = 0.0
    additional_charges: Optional[float] = 0.0


class BillModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
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


class PaymentModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    bill_id: str
    client_name: str
    amount: float
    payment_method: str
    payment_date: datetime
    reference: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime