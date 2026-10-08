"""Read-only, redacted development diagnostic for a local table session.

Usage: python -m scripts.inspect_table_session T20
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path


DEFAULT_DB = Path(__file__).resolve().parents[1] / ".local" / "table-sessions.sqlite3"


def inspect(table: str, db_path: Path = DEFAULT_DB) -> list[str]:
    if not db_path.is_file():
        raise FileNotFoundError("Local table-session database was not found")
    lines: list[str] = []
    with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        sessions = db.execute(
            "SELECT session_id,status,clover_order_id,operation_state "
            "FROM table_sessions WHERE table_id=? ORDER BY created_at,session_id",
            (table.strip().upper(),),
        ).fetchall()
        for session in sessions:
            order_id = session["clover_order_id"]
            suffix = order_id[-4:] if order_id else "none"
            lines.append(
                f"session_id={session['session_id']} status={session['status']} "
                f"operation_state={session['operation_state']} order_suffix={suffix}"
            )
            mutations = db.execute(
                "SELECT state,response_json FROM table_session_mutations "
                "WHERE session_id=? ORDER BY rowid",
                (session["session_id"],),
            ).fetchall()
            lines.append(f"mutation_count={len(mutations)}")
            for number, mutation in enumerate(mutations, 1):
                try:
                    metadata = json.loads(mutation["response_json"] or "{}")
                    metadata = metadata if isinstance(metadata, dict) else {}
                except (TypeError, ValueError):
                    metadata = {}
                phase = metadata.get("phase")
                resume = metadata.get("resume_phase")
                confirmed = (mutation["state"] == "applied" and order_id is not None
                             and (phase in ("atomic_created", "total_confirmed") or
                                  (phase == "readback_pending" and
                                   resume in ("atomic_created", "total_confirmed"))))
                safe_phase = phase if phase in (
                    "atomic_created", "append_applied", "total_pending",
                    "total_confirmed", "readback_pending",
                ) else "unknown"
                safe_resume = resume if resume in ("atomic_created", "total_confirmed") else "none"
                target_total = metadata.get("target_total_cents")
                safe_total = target_total if type(target_total) is int and target_total >= 0 else "none"
                has_modifiers = metadata.get("has_modifiers")
                safe_modifiers = has_modifiers if type(has_modifiers) is bool else "unknown"
                lines.append(
                    f"mutation_{number} state={mutation['state']} phase={safe_phase} "
                    f"resume_phase={safe_resume} confirmed_readback_only={confirmed} "
                    f"ambiguous={mutation['state'] == 'uncertain'} "
                    f"metadata_present={mutation['response_json'] is not None} "
                    f"expected_total_cents={safe_total} "
                    f"submitted_modifiers={safe_modifiers}"
                )
    if not lines:
        lines.append("No matching local table session")
    return lines


def main() -> None:
    if os.environ.get("APP_ENV", "development") != "development":
        raise SystemExit("This diagnostic is development-only")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table", help="Local table label, for example T20")
    arguments = parser.parse_args()
    for line in inspect(arguments.table):
        print(line)


if __name__ == "__main__":
    main()
