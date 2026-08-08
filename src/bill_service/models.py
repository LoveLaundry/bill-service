from datetime import datetime
from typing import Annotated, List, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

PyObjectId = Annotated[str, BeforeValidator(str)]


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
    items: List[BillItemIn] = Field(min_length=1)
    notes: Optional[str] = None


class BillModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    quotation_id: str
    client_name: str
    quotation_title: Optional[str] = None
    items: List[BillItemOut]
    total_quantity: int
    total_amount: float
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class BillListResponse(BaseModel):
    items: List[BillModel]
    total: int