"""Public request and response models for restaurant table sessions."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from app.orders.models import OrderLineRequest
from app.orders.models import OrderModifierResponse
from app.table_sessions.store import TableSession


class CreateTableSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9_-]+$")

    @field_validator("table", mode="before")
    @classmethod
    def normalize_table(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class AddSessionItemsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[OrderLineRequest] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_items(self) -> "AddSessionItemsRequest":
        identities = [(line.item_id, tuple(sorted(line.modifier_ids))) for line in self.items]
        if len(identities) != len(set(identities)) or sum(line.quantity for line in self.items) > 100:
            raise ValueError("Items must be unique and total quantity at most 100")
        return self


class TableSessionResponse(BaseModel):
    session_id: str
    table: str
    status: str
    clover_order_id: str | None
    created_at: str
    updated_at: str
    closed_at: str | None

    @classmethod
    def from_record(cls, session: TableSession) -> "TableSessionResponse":
        return cls(**session.__dict__)


class SessionLineResponse(BaseModel):
    item_id: str | None
    name: str | None
    quantity: int
    unit_price: Decimal | None
    modifiers: list[OrderModifierResponse] = Field(default_factory=list)

    @field_serializer("unit_price", when_used="json")
    def serialize_money(self, value: Decimal | None) -> float | None:
        return None if value is None else float(value)


class SessionOrderResponse(BaseModel):
    id: str
    currency: str | None
    subtotal: Decimal | None
    tax: Decimal | None
    total: Decimal | None
    items: list[SessionLineResponse]

    @field_serializer("subtotal", "tax", "total", when_used="json")
    def serialize_money(self, value: Decimal | None) -> float | None:
        return None if value is None else float(value)


class SessionItemsResponse(TableSessionResponse):
    order: SessionOrderResponse
