"""Customer-safe categorized menu derived from Clover inventory and local presentation metadata."""

import logging
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_serializer

from app.integrations.clover.client import CloverClient
from app.integrations.clover.exceptions import CloverUnavailableError
from app.menu_import.models import Menu, key, load_menu

logger = logging.getLogger(__name__)
MASTER_PATH = Path(__file__).resolve().parents[2] / "data" / "chennai_cafe_menu.json"
PAGE_SIZE = 100


def major(cents: int) -> Decimal:
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


class MenuAvailability(BaseModel):
    type: str = "always"


class MenuModifierOption(BaseModel):
    id: str
    name: str
    price_delta: Decimal

    @field_serializer("price_delta", when_used="json")
    def serialize_delta(self, value: Decimal) -> float:
        return float(value)


class MenuModifierGroup(BaseModel):
    id: str
    name: str
    required: bool
    min_select: int
    max_select: int
    options: list[MenuModifierOption]


class MenuItem(BaseModel):
    id: str
    name: str
    price: Decimal
    currency: str = "USD"
    availability: MenuAvailability = Field(default_factory=MenuAvailability)
    modifier_groups: list[MenuModifierGroup] = Field(default_factory=list)
    description: str | None = None
    image_url: str | None = None
    dietary_flags: list[str] = Field(default_factory=list)

    @field_serializer("price", when_used="json")
    def serialize_price(self, value: Decimal) -> float:
        return float(value)


class MenuCategory(BaseModel):
    id: str
    name: str
    display_order: int
    items: list[MenuItem]


class MenuResponse(BaseModel):
    categories: list[MenuCategory]


@lru_cache(maxsize=1)
def master_menu() -> Menu:
    return load_menu(MASTER_PATH)


def _elements(value: object, field: str) -> list[dict]:
    if not isinstance(value, dict) or not isinstance(value.get("elements"), list):
        raise CloverUnavailableError("Clover menu is unavailable")
    rows = value["elements"]
    if len(rows) >= 100 or any(not isinstance(row, dict) for row in rows):
        # Nested expansions are capped at 100. An exactly full expansion could be truncated.
        raise CloverUnavailableError("Clover menu is unavailable")
    return rows


def _identifier(row: dict) -> str:
    value = row.get("id")
    if not isinstance(value, str) or not value or len(value) > 64:
        raise CloverUnavailableError("Clover menu is unavailable")
    return value


async def _paged(client: CloverClient, resource: str, *, parent_id: str | None = None,
                 expand: tuple[str, ...] = ()) -> list[dict]:
    rows: list[dict] = []
    for offset in range(0, 10000, PAGE_SIZE):
        page = await client.inventory_collection(resource, parent_id=parent_id,
                                                  limit=PAGE_SIZE, offset=offset, expand=expand)
        elements = page.get("elements")
        if not isinstance(elements, list) or len(elements) > PAGE_SIZE or any(not isinstance(row, dict) for row in elements):
            raise CloverUnavailableError("Clover menu is unavailable")
        rows.extend(elements)
        if len(elements) < PAGE_SIZE:
            return rows
    raise CloverUnavailableError("Clover menu is unavailable")


async def load_catalog(client: CloverClient, metadata: Menu | None = None) -> MenuResponse:
    metadata = metadata or master_menu()
    categories_raw = await _paged(client, "categories")
    items_raw = await _paged(client, "items", expand=("categories", "modifierGroups"))
    groups_raw = await _paged(client, "modifier_groups")
    categories: dict[str, MenuCategory] = {}
    names: set[str] = set()
    known_orders = {key(name): order for name, order in metadata.categories}
    last_order = len(known_orders)
    for category in categories_raw:
        category_id = _identifier(category)
        category_name = category.get("name")
        if not isinstance(category_name, str) or not category_name.strip() or category_id in categories:
            raise CloverUnavailableError("Clover menu is unavailable")
        names.add(key(category_name))
        categories[category_id] = MenuCategory(
            id=category_id, name=category_name,
            display_order=known_orders.get(key(category_name), last_order + 1), items=[],
        )
    groups = {_identifier(group): group for group in groups_raw}
    if len(groups) != len(groups_raw):
        raise CloverUnavailableError("Clover menu is unavailable")
    required_group_ids = {
        _identifier(reference)
        for item in items_raw
        for reference in _elements(item.get("modifierGroups"), "modifierGroups")
    }
    option_groups: dict[str, MenuModifierGroup] = {}
    for group_id in required_group_ids:
        group = groups.get(group_id)
        if group is None or not isinstance(group.get("name"), str):
            raise CloverUnavailableError("Clover menu is unavailable")
        minimum, maximum = group.get("minRequired"), group.get("maxAllowed")
        if type(minimum) is not int or type(maximum) is not int or minimum < 0 or maximum < minimum or maximum < 1:
            raise CloverUnavailableError("Clover menu is unavailable")
        options: list[MenuModifierOption] = []
        for option in await _paged(client, "modifiers", parent_id=group_id):
            option_id = _identifier(option)
            option_name, cents = option.get("name"), option.get("price")
            if not isinstance(option_name, str) or not option_name.strip() or type(cents) is not int or cents < 0:
                raise CloverUnavailableError("Clover menu is unavailable")
            if option.get("available") is False or option.get("deletedTime") is not None:
                continue
            options.append(MenuModifierOption(id=option_id, name=option_name, price_delta=major(cents)))
        if maximum > len(options) or minimum > len(options):
            raise CloverUnavailableError("Clover menu is unavailable")
        option_groups[group_id] = MenuModifierGroup(
            id=group_id, name=group["name"], required=minimum > 0,
            min_select=minimum, max_select=maximum, options=options,
        )
    item_metadata = {key(item.name): item for item in metadata.items}
    uncategorized: MenuCategory | None = None
    for raw in items_raw:
        item_id = _identifier(raw)
        item_name, cents = raw.get("name"), raw.get("price")
        if not isinstance(item_name, str) or not item_name.strip() or type(cents) is not int or cents < 0:
            raise CloverUnavailableError("Clover menu is unavailable")
        if raw.get("deletedTime") is not None or raw.get("hidden") is True or raw.get("available") is False:
            continue
        known = item_metadata.get(key(item_name))
        if known and known.price_cents != cents:
            logger.warning("DEV Clover menu price differs from master for item name=%s", item_name)
        category_ids = [_identifier(reference) for reference in _elements(raw.get("categories"), "categories")]
        group_ids = [_identifier(reference) for reference in _elements(raw.get("modifierGroups"), "modifierGroups")]
        if any(category_id not in categories for category_id in category_ids) or any(group_id not in option_groups for group_id in group_ids):
            raise CloverUnavailableError("Clover menu is unavailable")
        item = MenuItem(
            id=item_id, name=item_name, price=major(cents),
            availability=MenuAvailability(type=known.availability if known else "always"),
            modifier_groups=[option_groups[group_id] for group_id in group_ids],
        )
        if not category_ids:
            if uncategorized is None:
                uncategorized = MenuCategory(id="uncategorized", name="Uncategorized",
                                             display_order=last_order + 2, items=[])
            uncategorized.items.append(item)
        else:
            for category_id in category_ids:
                categories[category_id].items.append(item)
    ordered = sorted(categories.values(), key=lambda category: (category.display_order, category.name.casefold(), category.id))
    if uncategorized:
        ordered.append(uncategorized)
    return MenuResponse(categories=ordered)
