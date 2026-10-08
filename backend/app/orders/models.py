"""Public order contract and Clover response normalization."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_serializer, field_validator, model_validator


class OrderLineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    quantity: StrictInt = Field(ge=1, le=10)
    modifier_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("modifier_ids")
    @classmethod
    def validate_modifier_ids(cls, values: list[str]) -> list[str]:
        import re
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in values) or len(values) != len(set(values)):
            raise ValueError("Invalid modifier selection")
        return values


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str = Field(min_length=1, max_length=32)
    items: list[OrderLineRequest] = Field(min_length=1, max_length=20)

    @field_validator("table", mode="before")
    @classmethod
    def strip_table(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("table")
    @classmethod
    def validate_table(cls, value: str) -> str:
        if not value.isprintable():
            raise ValueError("Table contains invalid characters")
        return value

    @model_validator(mode="after")
    def validate_items(self) -> "OrderRequest":
        identities = [(item.item_id, tuple(sorted(item.modifier_ids))) for item in self.items]
        if len(identities) != len(set(identities)) or sum(item.quantity for item in self.items) > 100:
            raise ValueError("Order contains duplicate items or too many units")
        return self


class OrderLineResponse(BaseModel):
    item_id: str
    name: str
    quantity: int
    unit_price: Decimal
    modifiers: list["OrderModifierResponse"] = Field(default_factory=list)

    @field_serializer("unit_price", when_used="json")
    def serialize_money(self, value: Decimal) -> float:
        return float(value)


class OrderModifierResponse(BaseModel):
    id: str
    name: str
    price_delta: Decimal

    @field_serializer("price_delta", when_used="json")
    def serialize_money(self, value: Decimal) -> float:
        return float(value)


class OrderResponse(BaseModel):
    id: str
    table: str
    items: list[OrderLineResponse]
    currency: str | None
    subtotal: Decimal | None
    tax: Decimal | None
    total: Decimal | None

    @field_serializer("subtotal", "tax", "total", when_used="json")
    def serialize_money(self, value: Decimal | None) -> float | None:
        return None if value is None else float(value)


def cents_to_major(value: int | None) -> Decimal | None:
    return None if value is None else (Decimal(value) / Decimal(100)).quantize(Decimal("0.01"))
