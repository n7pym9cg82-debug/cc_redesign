from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class CloverInventoryItem(BaseModel):
    """Subset of a Clover inventory item consumed by this service."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    price: int = Field(ge=0)
    available: bool | None = None
    hidden: bool | None = None
    deletedTime: int | None = None
    priceType: str | None = None
    isAgeRestricted: bool | None = None
    modifierGroups: dict | None = None


class CloverInventoryResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    elements: list[CloverInventoryItem] = Field(default_factory=list)


class CloverOrderType(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    isDeleted: bool | None = None
    isHidden: bool | None = None


class CloverOrderTypeListing(BaseModel):
    """Only documented fields needed by the development chooser."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    label: str | None = Field(default=None, max_length=128)
    isDefault: bool = False
    isHidden: bool = False
    customerIdMethod: str | None = Field(default=None, max_length=32)
    isDeleted: bool = False
    deletedTime: int | None = None


class CloverOrderTypesCollection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    elements: list[CloverOrderTypeListing]


class CloverLineItemReference(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    name: str | None = None


class CloverReadLineItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    item: CloverLineItemReference | None = None
    name: str | None = None
    price: int | None = Field(default=None, ge=0)
    unitQty: int | None = None
    modifications: "CloverReadModifications | None" = None


class CloverReadModification(BaseModel):
    model_config = ConfigDict(extra="ignore")
    modifier: CloverLineItemReference | None = None
    name: str | None = None
    amount: int | None = None


class CloverReadModifications(BaseModel):
    model_config = ConfigDict(extra="ignore")
    elements: list[CloverReadModification]


class CloverReadLineItems(BaseModel):
    model_config = ConfigDict(extra="ignore")

    elements: list[CloverReadLineItem]


class CloverOrderReadback(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    currency: str | None = None
    subtotal: int | None = Field(default=None, ge=0)
    totalTaxAmount: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    lineItems: CloverReadLineItems


class SafeOrderType(BaseModel):
    id: str
    label: str | None
    is_default: bool
    is_hidden: bool
    customer_id_method: str | None


class SafeOrderTypesResponse(BaseModel):
    order_types: list[SafeOrderType]


def normalize_order_types(types: list[CloverOrderTypeListing]) -> SafeOrderTypesResponse:
    return SafeOrderTypesResponse(
        order_types=[
            SafeOrderType(
                id=item.id,
                label=item.label,
                is_default=item.isDefault,
                is_hidden=item.isHidden,
                customer_id_method=item.customerIdMethod,
            )
            for item in types
            if not item.isDeleted and item.deletedTime is None
        ]
    )


class MenuItem(BaseModel):
    id: str
    name: str
    price: Decimal
    currency: str = "USD"

    @field_serializer("price", when_used="json")
    def serialize_price(self, value: Decimal) -> float:
        """Emit a JSON number only at the transport boundary."""
        return float(value)


class MenuResponse(BaseModel):
    items: list[MenuItem]


def normalize_inventory(response: CloverInventoryResponse) -> MenuResponse:
    cents_per_dollar = Decimal(100)
    items = [
        MenuItem(
            id=item.id,
            name=item.name,
            price=(Decimal(item.price) / cents_per_dollar).quantize(Decimal("0.01")),
        )
        for item in response.elements
    ]
    return MenuResponse(items=items)

