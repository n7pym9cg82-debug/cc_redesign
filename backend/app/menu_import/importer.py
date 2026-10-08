"""Read/plan/apply engine. No mutation occurs during discovery or planning."""

from dataclasses import dataclass, field

from app.core.config import SANDBOX_API_URL, Settings
from app.integrations.clover.client import CloverClient
from app.menu_import.models import Menu, key


class ImportBlocked(ValueError):
    pass


class ImportPartial(ImportBlocked):
    def __init__(self, progress: list[str], condition: str) -> None:
        super().__init__(condition)
        self.progress = tuple(progress)


@dataclass
class Snapshot:
    categories: dict[str, dict]
    items: dict[str, dict]
    groups: dict[str, dict]
    modifiers: dict[str, dict[str, dict]]
    item_details: dict[str, dict]
    errors: list[str] = field(default_factory=list)


@dataclass
class Plan:
    creates: dict[str, list[str]] = field(default_factory=lambda: {
        "categories": [], "modifier_groups": [], "modifiers": [], "items": [],
        "category_associations": [], "modifier_group_associations": [],
    })
    matching_items: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    existing: dict[str, int] = field(default_factory=dict)

    @property
    def can_apply(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict:
        return {
            "existing": self.existing, "planned": self.creates,
            "matching_items": self.matching_items,
            "blockers": self.blockers, "warnings": self.warnings,
            "can_apply": self.can_apply,
        }


def _named(rows: list[dict], kind: str, errors: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or not isinstance(row.get("name"), str) or not row["name"].strip():
            errors.append(f"Unsupported Clover {kind} schema")
            continue
        normalized = key(row["name"])
        if normalized in result:
            errors.append(f"Ambiguous Clover {kind} name: {row['name']}")
        else:
            result[normalized] = row
    return result


async def _pages(client: CloverClient, resource: str, parent_id: str | None = None) -> list[dict]:
    rows: list[dict] = []
    page_size = 100
    for offset in range(0, 10000, page_size):
        page = await client.inventory_collection(resource, parent_id=parent_id, limit=page_size, offset=offset)
        elements = page["elements"]
        if not isinstance(elements, list) or len(elements) > page_size or any(not isinstance(row, dict) for row in elements):
            raise ImportBlocked("Unsupported Clover collection schema")
        rows.extend(elements)
        if len(elements) < page_size:
            return rows
    raise ImportBlocked("Clover collection exceeds supported pagination limit")


async def discover(client: CloverClient, menu: Menu) -> Snapshot:
    errors: list[str] = []
    categories = _named(await _pages(client, "categories"), "category", errors)
    items = _named(await _pages(client, "items"), "item", errors)
    groups = _named(await _pages(client, "modifier_groups"), "modifier group", errors)
    modifiers: dict[str, dict[str, dict]] = {}
    for group in menu.groups:
        existing = groups.get(key(group.name))
        if existing:
            modifiers[key(group.name)] = _named(
                await _pages(client, "modifiers", existing["id"]), "modifier", errors,
            )
    details: dict[str, dict] = {}
    for item in menu.items:
        existing = items.get(key(item.name))
        if existing:
            details[key(item.name)] = await client.inventory_item(existing["id"])
    return Snapshot(categories, items, groups, modifiers, details, errors)


def _expanded_ids(detail: dict, field: str, blockers: list[str], item_name: str) -> set[str]:
    collection = detail.get(field)
    if not isinstance(collection, dict) or not isinstance(collection.get("elements"), list):
        blockers.append(f"Unsupported {field} expansion for {item_name}")
        return set()
    elements = collection["elements"]
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in elements):
        blockers.append(f"Unsupported {field} expansion for {item_name}")
        return set()
    # Clover expansions cap nested collections at 100. A full expansion is
    # indistinguishable from a truncated one, so fail closed.
    if len(elements) >= 100:
        blockers.append(f"Potentially truncated {field} expansion for {item_name}")
    return {row["id"] for row in elements}


def plan(menu: Menu, current: Snapshot) -> Plan:
    result = Plan()
    result.existing = {
        "categories": len(current.categories), "items": len(current.items),
        "modifier_groups": len(current.groups),
    }
    result.blockers.extend(current.errors)
    if any(item.availability == "weekend" for item in menu.items):
        result.warnings.append("Weekend availability not enforced in Clover.")
    for category_name, _ in menu.categories:
        if key(category_name) not in current.categories:
            result.creates["categories"].append(category_name)
    for group in menu.groups:
        if group.pending:
            result.blockers.append(f"{group.name}: options require confirmation")
            continue
        existing = current.groups.get(key(group.name))
        if existing is None:
            result.creates["modifier_groups"].append(group.name)
        else:
            if existing.get("minRequired") != int(group.required) or existing.get("maxAllowed") != group.max_select:
                result.blockers.append(f"Modifier group constraints conflict: {group.name}")
        existing_options = current.modifiers.get(key(group.name), {})
        for option_name, cents in group.options:
            option = existing_options.get(key(option_name))
            if option is None:
                result.creates["modifiers"].append(f"{group.name} / {option_name}")
            elif type(option.get("price")) is not int or option["price"] != cents:
                result.blockers.append(f"Modifier price conflict: {group.name} / {option_name}")
    for item in menu.items:
        existing = current.items.get(key(item.name))
        if existing is None:
            result.creates["items"].append(item.name)
            result.creates["category_associations"].append(item.name)
            if item.modifier_group and not next(g.pending for g in menu.groups if key(g.name) == key(item.modifier_group)):
                result.creates["modifier_group_associations"].append(item.name)
            continue
        if type(existing.get("price")) is not int:
            result.blockers.append(f"Unsupported Clover item price: {item.name}")
            continue
        if existing["price"] != item.price_cents:
            result.blockers.append(f"Item price conflict: {item.name}")
            continue
        result.matching_items.append(item.name)
        detail = current.item_details.get(key(item.name), {})
        if detail.get("price") != existing["price"] or detail.get("name") != existing["name"]:
            result.blockers.append(f"Inconsistent Clover item readback: {item.name}")
        category_ids = _expanded_ids(detail, "categories", result.blockers, item.name)
        category = current.categories.get(key(item.category))
        if category is None or category["id"] not in category_ids:
            if category_ids:
                result.blockers.append(f"Existing category differs for {item.name}; manual review required")
            else:
                result.creates["category_associations"].append(item.name)
        if item.modifier_group:
            group = current.groups.get(key(item.modifier_group))
            group_ids = _expanded_ids(detail, "modifierGroups", result.blockers, item.name)
            if group is None or group["id"] not in group_ids:
                if group_ids:
                    result.blockers.append(f"Existing modifier groups differ for {item.name}; manual review required")
                elif not next(g.pending for g in menu.groups if key(g.name) == key(item.modifier_group)):
                    result.creates["modifier_group_associations"].append(item.name)
    return result


def require_apply_guard(settings: Settings, confirmation: str | None) -> None:
    if confirmation != "APPLY_SANDBOX_MENU":
        raise ImportBlocked("Apply requires --confirm APPLY_SANDBOX_MENU")
    if settings.app_env != "development" or settings.clover_env != "sandbox" or str(settings.clover_base_url).rstrip("/") != SANDBOX_API_URL:
        raise ImportBlocked("Apply requires the exact development Clover Sandbox configuration")


async def apply(client: CloverClient, menu: Menu, settings: Settings, confirmation: str | None) -> dict:
    require_apply_guard(settings, confirmation)
    progress: list[str] = []
    initial = plan(menu, await discover(client, menu))
    if not initial.can_apply:
        raise ImportBlocked("Import plan has blockers; no writes were made")
    try:
        current = await discover(client, menu)
        for category, order in menu.categories:
            if key(category) not in current.categories:
                await client.inventory_create("categories", {"name": category, "sortOrder": order})
                progress.append(f"Category POST accepted: {category}")
        current = await discover(client, menu)
        if any(key(name) not in current.categories for name, _ in menu.categories):
            raise ImportBlocked("Category creation could not be verified")
        for group in menu.groups:
            if key(group.name) not in current.groups:
                await client.inventory_create("modifier_groups", {"name": group.name, "minRequired": int(group.required), "maxAllowed": group.max_select})
                progress.append(f"Modifier group POST accepted: {group.name}")
        current = await discover(client, menu)
        for group in menu.groups:
            existing = current.groups.get(key(group.name))
            if existing is None:
                raise ImportBlocked("Modifier group creation could not be verified")
            for option_name, cents in group.options:
                if key(option_name) not in current.modifiers.get(key(group.name), {}):
                    await client.inventory_create_modifier(existing["id"], {"name": option_name, "price": cents})
                    progress.append(f"Modifier POST accepted: {group.name} / {option_name}")
        current = await discover(client, menu)
        if any(key(option_name) not in current.modifiers.get(key(group.name), {}) for group in menu.groups for option_name, _ in group.options):
            raise ImportBlocked("Modifier creation could not be verified")
        for item in menu.items:
            if key(item.name) not in current.items:
                await client.inventory_create("items", {"name": item.name, "price": item.price_cents, "priceType": "FIXED"})
                progress.append(f"Item POST accepted: {item.name}")
        current = await discover(client, menu)
        interim = plan(menu, current)
        if interim.blockers:
            raise ImportBlocked("Clover state changed during apply; reconcile before retry")
        for item in menu.items:
            if item.name in interim.creates["category_associations"]:
                await client.inventory_create("category_items", {"elements": [{"category": {"id": current.categories[key(item.category)]["id"]}, "item": {"id": current.items[key(item.name)]["id"]}}]})
                progress.append(f"Category association POST accepted: {item.name}")
        current = await discover(client, menu)
        interim = plan(menu, current)
        if interim.blockers:
            raise ImportBlocked("Clover state changed during apply; reconcile before retry")
        for item in menu.items:
            if item.name in interim.creates["modifier_group_associations"]:
                await client.inventory_create("item_modifier_groups", {"elements": [{"modifierGroup": {"id": current.groups[key(item.modifier_group)]["id"]}, "item": {"id": current.items[key(item.name)]["id"]}}]})
                progress.append(f"Modifier association POST accepted: {item.name}")
        final = plan(menu, await discover(client, menu))
        if final.blockers or any(final.creates.values()):
            raise ImportBlocked("Post-import verification did not match the master menu")
        return {"status": "verified", "progress": progress, "plan": final.as_dict()}
    except Exception as exc:
        raise ImportPartial(progress, f"Import stopped after {len(progress)} accepted POST responses; run a new dry run and reconcile. Last condition: {type(exc).__name__}") from exc
