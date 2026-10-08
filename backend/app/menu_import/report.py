"""Human and machine-readable import reports (no credentials or raw payloads)."""

import json
from datetime import datetime, timezone
from pathlib import Path

from app.menu_import.importer import Plan


def render(plan: Plan, *, applied: bool = False) -> str:
    lines = ["MENU IMPORT APPLY" if applied else "MENU IMPORT DRY RUN", "", "Existing Clover:"]
    lines.extend(f"  {kind}: {count}" for kind, count in plan.existing.items())
    lines.extend(["", "Planned:"])
    lines.extend(f"  {kind}: {len(names)}" for kind, names in plan.creates.items())
    lines.append(f"  items already matching: {len(plan.matching_items)}")
    lines.extend(["", "BLOCKERS"])
    lines.extend(f"- {message}" for message in plan.blockers)
    lines.extend(["", "WARNINGS"])
    lines.extend(f"- {message}" for message in plan.warnings)
    lines.extend(["", "No Clover changes were made." if not applied else "Apply verified against Clover."])
    return "\n".join(lines)


def save_report(plan: Plan, root: Path, *, applied: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = root / f"menu-import-{stamp}.json"
    path.write_text(json.dumps({"mode": "apply" if applied else "dry-run", **plan.as_dict()}, indent=2) + "\n", encoding="utf-8")
    return path
