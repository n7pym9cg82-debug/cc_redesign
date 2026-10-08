"""Validated source menu. Source prices remain strings until Decimal conversion."""

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


class MenuValidationError(ValueError):
    pass


def key(name: str) -> str:
    return " ".join(name.split()).casefold()


def money(value: object) -> int:
    if not isinstance(value, str):
        raise MenuValidationError("Prices must be decimal strings")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise MenuValidationError("Invalid price") from None
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
        raise MenuValidationError("Invalid price precision or range")
    cents = amount * 100
    if cents > 2_147_483_647:
        raise MenuValidationError("Price exceeds supported Clover range")
    return int(cents)


def name(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise MenuValidationError("Invalid menu name")
    return value


@dataclass(frozen=True)
class Item:
    name: str
    price_cents: int
    category: str
    availability: str = "always"
    modifier_group: str | None = None


@dataclass(frozen=True)
class Group:
    name: str
    required: bool
    max_select: int
    options: tuple[tuple[str, int], ...]
    pending: bool = False


@dataclass(frozen=True)
class Menu:
    categories: tuple[tuple[str, int], ...]
    items: tuple[Item, ...]
    groups: tuple[Group, ...]


def _unique(values: list[str], kind: str) -> None:
    if len({key(value) for value in values}) != len(values):
        raise MenuValidationError(f"Duplicate desired {kind} name")


def parse_menu(raw: object) -> Menu:
    if not isinstance(raw, dict) or set(raw) != {"categories", "modifier_groups"}:
        raise MenuValidationError("Unsupported master menu schema")
    categories: list[tuple[str, int]] = []
    items: list[Item] = []
    groups: list[Group] = []
    if not isinstance(raw["categories"], list) or not isinstance(raw["modifier_groups"], list):
        raise MenuValidationError("Invalid master menu collections")
    for category in raw["categories"]:
        if not isinstance(category, dict) or set(category) != {"name", "display_order", "items"}:
            raise MenuValidationError("Invalid category schema")
        category_name = name(category["name"])
        order = category["display_order"]
        if type(order) is not int or order < 1 or not isinstance(category["items"], list):
            raise MenuValidationError("Invalid category order/items")
        categories.append((category_name, order))
        for row in category["items"]:
            if not isinstance(row, list) or not 2 <= len(row) <= 4:
                raise MenuValidationError("Invalid item schema")
            availability = row[2] if len(row) >= 3 else "always"
            group = name(row[3]) if len(row) == 4 else None
            if availability not in ("always", "weekend"):
                raise MenuValidationError("Invalid item availability")
            items.append(Item(name(row[0]), money(row[1]), category_name, availability, group))
    _unique([n for n, _ in categories], "category")
    _unique([item.name for item in items], "item")
    if sorted(order for _, order in categories) != list(range(1, len(categories) + 1)):
        raise MenuValidationError("Category display order must be contiguous")
    for raw_group in raw["modifier_groups"]:
        if not isinstance(raw_group, dict) or not {"name", "required", "max_select", "options"} <= set(raw_group) or set(raw_group) - {"name", "required", "max_select", "options", "options_pending_confirmation"}:
            raise MenuValidationError("Invalid modifier group schema")
        group_name = name(raw_group["name"])
        required = raw_group["required"]
        maximum = raw_group["max_select"]
        pending = raw_group.get("options_pending_confirmation", False)
        if type(required) is not bool or type(maximum) is not int or maximum < 1 or type(pending) is not bool or not isinstance(raw_group["options"], list):
            raise MenuValidationError("Invalid modifier group constraints")
        options: list[tuple[str, int]] = []
        for row in raw_group["options"]:
            if not isinstance(row, list) or len(row) != 2:
                raise MenuValidationError("Invalid modifier schema")
            options.append((name(row[0]), money(row[1])))
        _unique([n for n, _ in options], "modifier")
        if (pending and options) or (not pending and required and not options):
            raise MenuValidationError("Required modifier options are unresolved")
        groups.append(Group(group_name, required, maximum, tuple(options), pending))
    _unique([group.name for group in groups], "modifier group")
    group_names = {key(group.name) for group in groups}
    if any(item.modifier_group and key(item.modifier_group) not in group_names for item in items):
        raise MenuValidationError("Invalid item modifier-group reference")
    return Menu(tuple(categories), tuple(items), tuple(groups))


def load_menu(path: Path) -> Menu:
    try:
        return parse_menu(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise MenuValidationError("Master menu is unavailable or invalid JSON") from exc
