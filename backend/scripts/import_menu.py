"""CLI for a guarded one-time Clover Sandbox inventory import."""

import argparse
import asyncio
from pathlib import Path

from app.core.config import SANDBOX_API_URL, Settings
from app.integrations.clover.client import CloverClient
from app.integrations.clover.oauth_service import OAuthService
from app.integrations.clover.token_store import SQLiteTokenStore
from app.menu_import.importer import ImportBlocked, ImportPartial, apply, discover, plan, require_apply_guard
from app.menu_import.models import load_menu
from app.menu_import.report import render, save_report

BACKEND = Path(__file__).resolve().parents[1]
MASTER = BACKEND / "data" / "chennai_cafe_menu.json"
REPORTS = BACKEND / ".local" / "menu-import"


async def run(settings: Settings, *, do_apply: bool, confirmation: str | None, master: Path = MASTER, report_root: Path = REPORTS) -> int:
    # All guards run before obtaining a token or opening an HTTP client.
    if do_apply:
        require_apply_guard(settings, confirmation)
    if settings.app_env != "development" or settings.clover_env != "sandbox" or str(settings.clover_base_url).rstrip("/") != SANDBOX_API_URL:
        raise ImportBlocked("Importer requires the exact development Clover Sandbox configuration")
    menu = load_menu(master)
    if settings.clover_auth_mode == "oauth":
        config = settings.require_oauth_configuration()
        store = SQLiteTokenStore(config.database_path, config.encryption_key, app_env=settings.app_env, environment=settings.clover_env)
        service = OAuthService(config, store)
        async with service.http_client_factory() as http:
            async with CloverClient(base_url=SANDBOX_API_URL, merchant_id=config.identity.merchant_id, http_client=http, token_provider=service.token_provider) as client:
                return await _execute(client, menu, settings, do_apply, confirmation, report_root)
    merchant_id, token = settings.require_inventory_credentials()
    async with CloverClient(base_url=SANDBOX_API_URL, merchant_id=merchant_id, api_token=token) as client:
        return await _execute(client, menu, settings, do_apply, confirmation, report_root)


async def _execute(client: CloverClient, menu, settings: Settings, do_apply: bool, confirmation: str | None, root: Path) -> int:
    current = plan(menu, await discover(client, menu))
    print(render(current))
    print(f"JSON report: {save_report(current, root)}")
    if not do_apply:
        return 0
    if not current.can_apply:
        raise ImportBlocked("Apply blocked by dry-run findings; no Clover changes were made")
    try:
        result = await apply(client, menu, settings, confirmation)
    except ImportPartial as exc:
        root.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        import json
        path = root / f"menu-import-partial-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        path.write_text(json.dumps({"status": "reconciliation_required", "confirmed_operations": exc.progress}, indent=2) + "\n", encoding="utf-8")
        print(f"Partial progress report: {path}")
        raise
    final = plan(menu, await discover(client, menu))
    print(render(final, applied=True))
    print(f"JSON report: {save_report(final, root, applied=True)}")
    print(f"Accepted POST responses: {len(result['progress'])}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Clover Sandbox menu importer; defaults to dry run")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    try:
        return asyncio.run(run(Settings(), do_apply=args.apply, confirmation=args.confirm))
    except Exception as exc:
        # Never include provider payloads or settings in terminal output.
        print(f"Menu import stopped: {type(exc).__name__}. Review configuration, blockers, or a fresh dry run.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
