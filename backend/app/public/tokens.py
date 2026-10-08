"""Opaque authenticated QR table tokens using maintained Fernet primitives."""

import json
import re

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import Settings


class TableTokenError(ValueError):
    pass


class TableTokenService:
    def __init__(self, settings: Settings) -> None:
        if not settings.clover_merchant_id or not re.fullmatch(
            r"[A-Za-z0-9_-]+", settings.clover_merchant_id
        ):
            raise TableTokenError("Table tokens are not configured")
        self._merchant_id = settings.clover_merchant_id
        secret = settings.table_token_signing_key
        if secret is None:
            raise TableTokenError("Table tokens are not configured")
        try:
            keys = [Fernet(secret.get_secret_value().encode("ascii"))]
            previous = settings.table_token_previous_keys
            if previous is not None:
                keys.extend(
                    Fernet(value.strip().encode("ascii"))
                    for value in previous.get_secret_value().split(",") if value.strip()
                )
            self._fernet = MultiFernet(keys)
        except (ValueError, UnicodeError):
            raise TableTokenError("Table tokens are not configured") from None
        self.version = settings.table_token_version

    def issue(self, table: str) -> str:
        normalized = table.strip().upper()
        if not re.fullmatch(r"[A-Z0-9_-]{1,32}", normalized):
            raise TableTokenError("Invalid table")
        payload = json.dumps(
            {"version": self.version, "table": normalized, "merchant": self._merchant_id},
            separators=(",", ":"),
        ).encode("ascii")
        return self._fernet.encrypt(payload).decode("ascii")

    def resolve(self, token: str) -> str:
        # GET path parameters are URL-decoded by ASGI, but JSON body strings are
        # not. A printed QR URL escapes Fernet's trailing '=' padding as %3D.
        # Decode only that padding representation, not arbitrary percent escapes.
        if len(token) > 512:
            raise TableTokenError("Invalid table token")
        canonical = re.sub(r"(?i)%3d", "=", token)
        if not re.fullmatch(r"[A-Za-z0-9_=-]+", canonical):
            raise TableTokenError("Invalid table token")
        try:
            data = json.loads(self._fernet.decrypt(canonical.encode("ascii")))
        except (InvalidToken, ValueError, UnicodeError):
            raise TableTokenError("Invalid table token") from None
        if (not isinstance(data, dict) or data.get("version") != self.version
                or data.get("merchant") != self._merchant_id):
            raise TableTokenError("Invalid table token")
        table = data.get("table")
        if not isinstance(table, str) or not re.fullmatch(r"[A-Z0-9_-]{1,32}", table):
            raise TableTokenError("Invalid table token")
        return table


def display_table(table: str) -> str:
    match = re.fullmatch(r"T(\d+)", table)
    return f"Table {match.group(1)}" if match else table
