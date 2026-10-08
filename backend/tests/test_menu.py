"""Categorized menu is built from synthetic Clover inventory only."""

import httpx
import pytest
from decimal import Decimal

from app.api.menu import get_clover_client
from app.core.config import Settings, get_settings
from app.main import app
from app.menu_import.catalog import load_catalog, master_menu


class CatalogClient:
    merchant_id = "synthetic-merchant"

    def __init__(self):
        self.calls = []
        self.categories = [
            {"id": "C2", "name": "Biryani"}, {"id": "C1", "name": "Starters"},
            {"id": "CX", "name": "Chef Extras"},
        ]
        self.items = [
            {"id": "I1", "name": "Goat Biryani", "price": 2099,
             "categories": {"elements": [{"id": "C2"}]},
             "modifierGroups": {"elements": []}},
            {"id": "I2", "name": "Lassi", "price": 399,
             "categories": {"elements": [{"id": "CX"}]},
             "modifierGroups": {"elements": [{"id": "G1"}]}},
        ]
        self.modifier_groups = [{"id": "G1", "name": "Lassi Style", "minRequired": 1, "maxAllowed": 1}]
        self.modifiers = {"G1": [
            {"id": "M1", "name": "Plain", "price": 0},
            {"id": "M2", "name": "Sweet", "price": 25},
        ]}

    async def inventory_collection(self, resource, *, parent_id=None, limit=100, offset=0, expand=()):
        self.calls.append((resource, parent_id, offset, expand))
        rows = self.modifiers[parent_id] if resource == "modifiers" else getattr(self, resource)
        return {"elements": rows[offset:offset + limit]}


@pytest.mark.asyncio
async def test_catalog_orders_categories_and_uses_clover_relationships_and_prices(caplog):
    client = CatalogClient()
    with caplog.at_level("WARNING"):
        menu = await load_catalog(client, master_menu())
    assert [category.name for category in menu.categories] == ["Starters", "Biryani", "Chef Extras"]
    assert [category.display_order for category in menu.categories] == [2, 7, 16]
    assert menu.categories[1].items[0].name == "Goat Biryani"
    assert menu.categories[1].items[0].availability.type == "weekend"
    assert menu.categories[1].items[0].price == Decimal("20.99")  # Clover overrides master 19.99.
    assert "price differs" in caplog.text and "synthetic-token" not in caplog.text
    lassi = menu.categories[2].items[0]
    assert lassi.modifier_groups[0].id == "G1"
    assert lassi.modifier_groups[0].required is True
    assert lassi.modifier_groups[0].max_select == 1
    assert [(option.id, option.price_delta) for option in lassi.modifier_groups[0].options] == [("M1", Decimal("0.00")), ("M2", Decimal("0.25"))]
    assert len([call for call in client.calls if call[0] == "items"]) == 1
    assert len(client.calls) == 4  # Three list reads plus one group option list, never per item.


@pytest.mark.asyncio
async def test_menu_route_cache_hit_and_expiry(monkeypatch):
    from app.api import menu as menu_module
    client = CatalogClient()
    app.dependency_overrides[get_clover_client] = lambda: client
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, menu_cache_ttl_seconds=60)
    now = [100.0]
    monkeypatch.setattr(menu_module.time, "monotonic", lambda: now[0])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
        first = await browser.get("/api/menu")
        second = await browser.get("/api/menu")
        now[0] = 161.0
        third = await browser.get("/api/menu")
    assert first.status_code == second.status_code == third.status_code == 200
    assert len(client.calls) == 8
    assert first.json()["categories"][1]["items"][0]["price"] == 20.99


@pytest.mark.asyncio
async def test_missing_inventory_credentials_return_sanitized_503():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
        response = await browser.get("/api/menu")
    assert response.status_code == 503
    assert response.json() == {"detail": "Clover integration is not configured"}


@pytest.mark.asyncio
async def test_catalog_rejects_malformed_relationship_without_misassigning():
    client = CatalogClient()
    client.items[1]["modifierGroups"] = {"elements": [{"id": "UNKNOWN"}]}
    with pytest.raises(Exception):
        await load_catalog(client)


@pytest.mark.asyncio
async def test_catalog_paginates_129_items_without_per_item_reads():
    client = CatalogClient()
    client.categories = [{"id": f"C{index}", "name": name} for index, (name, _) in enumerate(master_menu().categories)]
    client.items = [{"id": f"I{index}", "name": f"Synthetic {index}", "price": 100,
                     "categories": {"elements": [{"id": "C0"}]},
                     "modifierGroups": {"elements": []}} for index in range(129)]
    client.modifier_groups = []
    result = await load_catalog(client)
    assert len(result.categories) == 15
    assert [category.display_order for category in result.categories] == list(range(1, 16))
    assert len(result.categories[0].items) == 129
    assert [call[2] for call in client.calls if call[0] == "items"] == [0, 100]
    assert len(client.calls) == 4  # Categories, two item pages, groups.
