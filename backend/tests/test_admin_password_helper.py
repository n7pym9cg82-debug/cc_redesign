"""The interactive hash helper fails clearly without exposing entered passwords."""

import pytest

from app.admin.auth import verify_password
from scripts import hash_admin_password


def test_short_password_has_friendly_error(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "short"

    monkeypatch.setattr(hash_admin_password, "getpass", answer)
    with pytest.raises(SystemExit, match="at least 12 characters"):
        hash_admin_password.main()
    assert len(prompts) == 1


def test_mismatch_has_friendly_error(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(["synthetic-password", "different-password"])
    monkeypatch.setattr(hash_admin_password, "getpass", lambda _prompt: next(responses))
    with pytest.raises(SystemExit, match="Passwords did not match"):
        hash_admin_password.main()


def test_valid_password_prints_only_verifiable_hash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(hash_admin_password, "getpass", lambda _prompt: "synthetic-password")
    hash_admin_password.main()
    output = capsys.readouterr().out.strip()
    assert output.startswith("pbkdf2_sha256$")
    assert "synthetic-password" not in output
    assert verify_password("synthetic-password", output)
