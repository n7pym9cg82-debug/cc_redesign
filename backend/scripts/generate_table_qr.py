"""Generate a signed development QR URL and PNG for a physical table."""

import argparse
import re
from pathlib import Path
from urllib.parse import quote

import qrcode

from app.core.config import Settings
from app.public.tokens import TableTokenError, TableTokenService


def main(*, settings: Settings | None = None, output_dir: Path | None = None) -> Path:
    parser = argparse.ArgumentParser(description="Generate a development table QR")
    parser.add_argument("table", help="Normalized table ID, for example T12")
    args = parser.parse_args()
    settings = settings or Settings()
    if settings.app_env != "development" or settings.clover_env != "sandbox":
        parser.error("QR generation is restricted to development Sandbox")
    if settings.frontend_public_url is None:
        parser.error("FRONTEND_PUBLIC_URL is required")
    table = args.table.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,32}", table):
        parser.error("Invalid table ID")
    try:
        token = TableTokenService(settings).issue(table)
    except TableTokenError:
        parser.error("TABLE_TOKEN_SIGNING_KEY is not configured correctly")
    url = f"{str(settings.frontend_public_url).rstrip('/')}/order/{quote(token, safe='')}"
    output_dir = output_dir or Path(__file__).resolve().parents[1] / ".local" / "qr"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{table}-v{settings.table_token_version}.png"
    qrcode.make(url).save(output)
    print(f"Table token: {token}")
    print(f"Development URL: {url}")
    print(f"QR PNG: {output}")
    return output


if __name__ == "__main__":
    main()
