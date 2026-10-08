import copy
import json
from pathlib import Path

import httpx
import pytest

from app.core.config import SANDBOX_API_URL, Settings
from app.integrations.clover.client import CloverClient
from app.menu_import.importer import ImportBlocked, ImportPartial, Snapshot, apply, discover, plan, require_apply_guard
from app.menu_import.models import MenuValidationError, load_menu, money, parse_menu
from scripts import import_menu as cli

MASTER = Path(__file__).resolve().parents[1] / "data" / "chennai_cafe_menu.json"


def source():
    return json.loads(MASTER.read_text(encoding="utf-8"))


def tiny_source():
    return {"categories": [{"name": "Beverages", "display_order": 1, "items": [["Lassi", "3.99", "always", "Lassi Style"]]}], "modifier_groups": [{"name": "Lassi Style", "required": True, "max_select": 1, "options": [["Sweet", "0.00"]]}]}


def empty():
    return Snapshot({}, {}, {}, {}, {})


def test_master_counts_prices_and_confirmed_options():
    menu = load_menu(MASTER)
    assert (len(menu.categories), len(menu.items), len(menu.groups), sum(len(g.options) for g in menu.groups)) == (15, 129, 4, 16)
    assert [item.price_cents for item in menu.items if item.name in {"Masala Vada", "Chennai Cafe Special Thali"}] == [500, 2399]
    assert {item.name for item in menu.items if item.availability == "weekend"} == {"Tandoori Chicken", "Goat Biryani"}
    result = plan(menu, empty())
    assert len(result.creates["categories"]) == 15
    assert len(result.creates["items"]) == 129
    assert len(result.creates["modifier_groups"]) == 4
    assert len(result.creates["modifiers"]) == 16
    assert not result.blockers
    assert "Weekend availability not enforced in Clover." in result.warnings


@pytest.mark.parametrize("value", ["1.001", "-1.00", "NaN", "Infinity", 5.99, "abc"])
def test_invalid_money(value):
    with pytest.raises(MenuValidationError):
        money(value)


@pytest.mark.parametrize("kind", ["category", "item", "group", "modifier"])
def test_duplicate_desired_names(kind):
    raw = tiny_source()
    if kind == "category":
        raw["categories"].append({"name": " beverages ".strip().upper(), "display_order": 2, "items": []})
    elif kind == "item":
        raw["categories"][0]["items"].append(["LASSI", "3.99"])
    elif kind == "group":
        raw["modifier_groups"].append(copy.deepcopy(raw["modifier_groups"][0]))
    else:
        raw["modifier_groups"][0]["options"].append(["SWEET", "0.00"])
    with pytest.raises(MenuValidationError):
        parse_menu(raw)


def test_invalid_group_reference_and_price():
    raw = tiny_source()
    raw["categories"][0]["items"][0][3] = "Missing"
    with pytest.raises(MenuValidationError):
        parse_menu(raw)
    raw = tiny_source()
    raw["categories"][0]["items"][0][1] = "3.999"
    with pytest.raises(MenuValidationError):
        parse_menu(raw)


def test_matching_and_conflicting_plan():
    menu = parse_menu(tiny_source())
    current = Snapshot(
        {"beverages": {"id": "C1", "name": "BEVERAGES"}},
        {"lassi": {"id": "I1", "name": "Lassi", "price": 399}},
        {"lassi style": {"id": "G1", "name": "Lassi Style", "minRequired": 1, "maxAllowed": 1}},
        {"lassi style": {"sweet": {"id": "M1", "name": "Sweet", "price": 0}}},
        {"lassi": {"id": "I1", "name": "Lassi", "price": 399, "categories": {"elements": []}, "modifierGroups": {"elements": [{"id": "G1"}]}}},
    )
    result = plan(menu, current)
    assert result.creates["categories"] == []
    assert result.creates["items"] == []
    assert result.creates["modifier_groups"] == []
    assert result.creates["modifiers"] == []
    assert result.creates["category_associations"] == ["Lassi"]
    assert result.creates["modifier_group_associations"] == []
    current.items["lassi"]["price"] = 499
    assert any("Item price conflict" in blocker for blocker in plan(menu, current).blockers)


def test_missing_create_plan_and_modifier_matching():
    menu = parse_menu(tiny_source())
    result = plan(menu, empty())
    assert result.can_apply
    assert result.creates["categories"] == ["Beverages"]
    assert result.creates["items"] == ["Lassi"]
    assert result.creates["modifier_groups"] == ["Lassi Style"]
    assert result.creates["modifiers"] == ["Lassi Style / Sweet"]


def test_wrong_existing_category_blocks_unsafe_association():
    menu = parse_menu(tiny_source())
    current = Snapshot(
        {"beverages": {"id": "C1", "name": "Beverages"}},
        {"lassi": {"id": "I1", "name": "Lassi", "price": 399}},
        {}, {},
        {"lassi": {"id": "I1", "name": "Lassi", "price": 399,
                   "categories": {"elements": [{"id": "OTHER"}]},
                   "modifierGroups": {"elements": []}}},
    )
    result = plan(menu, current)
    assert any("Existing category differs" in x for x in result.blockers)
    assert result.creates["category_associations"] == []


def settings(**overrides):
    return Settings(_env_file=None, clover_merchant_id="TESTMERCHANT", clover_api_token="synthetic-token", **overrides)


@pytest.mark.parametrize("overrides", [{"app_env": "production"}, {"clover_env": "production"}, {"clover_base_url": "https://example.com"}])
def test_apply_environment_guard(overrides):
    with pytest.raises(ImportBlocked):
        require_apply_guard(settings(**overrides), "APPLY_SANDBOX_MENU")


def test_apply_confirmation_guard():
    with pytest.raises(ImportBlocked):
        require_apply_guard(settings(), None)
    with pytest.raises(ImportBlocked):
        require_apply_guard(settings(), "WRONG")


class FakeClover:
    def __init__(self, *, fail_post=False, hide_association=False):
        self.categories = []
        self.items = []
        self.modifier_groups = []
        self.modifiers = {}
        self.categories_by_item = {}
        self.groups_by_item = {}
        self.writes = []
        self.fail_post = fail_post
        self.hide_association = hide_association

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def inventory_collection(self, resource, *, parent_id=None, limit=100, offset=0):
        values = self.modifiers.get(parent_id, []) if resource == "modifiers" else getattr(self, resource)
        return {"elements": values[offset:offset + limit]}

    async def inventory_item(self, item_id):
        item = next(item for item in self.items if item["id"] == item_id)
        return {**item, "categories": {"elements": [{"id": x} for x in self.categories_by_item.get(item_id, set())]}, "modifierGroups": {"elements": [{"id": x} for x in self.groups_by_item.get(item_id, set())]}}

    async def inventory_create(self, resource, body):
        self.writes.append((resource, body))
        if self.fail_post:
            raise httpx.ReadTimeout("synthetic ambiguous write")
        if resource in ("categories", "items", "modifier_groups"):
            getattr(self, resource).append({"id": f"X{len(self.writes)}", **body})
        elif not self.hide_association:
            row = body["elements"][0]
            if resource == "category_items":
                self.categories_by_item.setdefault(row["item"]["id"], set()).add(row["category"]["id"])
            else:
                self.groups_by_item.setdefault(row["item"]["id"], set()).add(row["modifierGroup"]["id"])

    async def inventory_create_modifier(self, group_id, body):
        self.writes.append(("modifiers", body))
        self.modifiers.setdefault(group_id, []).append({"id": f"X{len(self.writes)}", **body})


@pytest.mark.asyncio
async def test_dry_run_zero_writes_and_apply_repeats_safely():
    menu = parse_menu(tiny_source())
    client = FakeClover()
    assert plan(menu, await discover(client, menu)).can_apply
    assert client.writes == []
    await apply(client, menu, settings(), "APPLY_SANDBOX_MENU")
    first_count = len(client.writes)
    assert first_count == 6
    assert not any(plan(menu, await discover(client, menu)).creates.values())
    await apply(client, menu, settings(), "APPLY_SANDBOX_MENU")
    assert len(client.writes) == first_count


@pytest.mark.asyncio
async def test_ambiguous_post_stops_without_retry():
    client = FakeClover(fail_post=True)
    with pytest.raises(ImportPartial) as exc:
        await apply(client, parse_menu(tiny_source()), settings(), "APPLY_SANDBOX_MENU")
    assert len(client.writes) == 1 and exc.value.progress == ()


@pytest.mark.asyncio
async def test_shipped_confirmed_group_is_planned_without_writes():
    client = FakeClover()
    result = plan(load_menu(MASTER), await discover(client, load_menu(MASTER)))
    assert result.can_apply
    assert "Chennai Cafe Special Thali – Dosa Choice" in result.creates["modifier_groups"]
    assert client.writes == []


@pytest.mark.asyncio
async def test_post_import_verification_detects_mismatch():
    client = FakeClover(hide_association=True)
    with pytest.raises(ImportPartial) as exc:
        await apply(client, parse_menu(tiny_source()), settings(), "APPLY_SANDBOX_MENU")
    assert len(exc.value.progress) > 0


@pytest.mark.asyncio
async def test_mock_transport_uses_trusted_merchant_and_never_writes():
    seen = []
    def handler(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.path.startswith("/v3/merchants/TESTMERCHANT/")
        assert request.headers["authorization"] == "Bearer synthetic-token"
        return httpx.Response(200, json={"elements": []})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with CloverClient(base_url=SANDBOX_API_URL, merchant_id="TESTMERCHANT", api_token="synthetic-token", http_client=http) as client:
            result = plan(parse_menu(tiny_source()), await discover(client, parse_menu(tiny_source())))
    assert result.can_apply and len(seen) == 3


@pytest.mark.asyncio
async def test_mocked_clover_wire_payloads_and_no_post_retry():
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 3:
            raise httpx.ReadTimeout("synthetic ambiguous outcome")
        return httpx.Response(200, json={"id": "X1"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with CloverClient(base_url=SANDBOX_API_URL, merchant_id="TESTMERCHANT", api_token="synthetic-token", http_client=http) as client:
            await client.inventory_create("items", {"name": "Lassi", "price": 399, "priceType": "FIXED"})
            await client.inventory_create("category_items", {"elements": [{"category": {"id": "C1"}, "item": {"id": "I1"}}]})
            with pytest.raises(Exception):
                await client.inventory_create_modifier("G1", {"name": "Sweet", "price": 0})
    assert len(seen) == 3
    assert all(request.url.path.startswith("/v3/merchants/TESTMERCHANT/") for request in seen)
    assert json.loads(seen[0].content) == {"name": "Lassi", "price": 399, "priceType": "FIXED"}
    assert json.loads(seen[1].content) == {"elements": [{"category": {"id": "C1"}, "item": {"id": "I1"}}]}
    assert json.loads(seen[2].content) == {"name": "Sweet", "price": 0}


@pytest.mark.asyncio
async def test_mocked_cli_dry_run_writes_only_local_report(monkeypatch, tmp_path, capsys):
    fake = FakeClover()
    monkeypatch.setattr(cli, "CloverClient", lambda **kwargs: fake)
    path = tmp_path / "menu.json"
    path.write_text(json.dumps(tiny_source()), encoding="utf-8")
    report_root = tmp_path / "reports"
    assert await cli.run(settings(), do_apply=False, confirmation=None, master=path, report_root=report_root) == 0
    assert fake.writes == []
    assert "MENU IMPORT DRY RUN" in capsys.readouterr().out
    assert len(list(report_root.glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_mocked_cli_apply_guard_precedes_client(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CloverClient", lambda **kwargs: pytest.fail("client constructed"))
    with pytest.raises(ImportBlocked):
        await cli.run(settings(), do_apply=True, confirmation="WRONG", master=tmp_path / "none")
