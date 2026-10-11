"""Generate the ADMIN_PASSWORD_HASH without putting a password in shell history."""

from getpass import getpass

from app.admin.auth import hash_password


def main() -> None:
    password = getpass("Admin password (at least 12 characters): ")
    if len(password) < 12:
        raise SystemExit("Admin password must be at least 12 characters. Run the command again.")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords did not match. Run the command again.")
    print(hash_password(password))


if __name__ == "__main__":
    main()
