"""Prompts for the shared password used to reach the app from other devices. Run via set_password.bat."""
import getpass
import sys

from app.auth import set_password, ACCESS_FILE

MIN_LENGTH = 8


def main() -> int:
    print("Choose the password you and your wife will use to open the app from other devices.")
    print(f"(At least {MIN_LENGTH} characters. Typing is hidden. Run this again any time to change it.)\n")
    first = getpass.getpass("New password: ")
    if len(first) < MIN_LENGTH:
        print(f"\nToo short -- needs at least {MIN_LENGTH} characters. Nothing was changed.")
        return 1
    if getpass.getpass("Type it again:  ") != first:
        print("\nThe two entries didn't match. Nothing was changed.")
        return 1
    set_password(first)
    print(f"\nPassword saved ({ACCESS_FILE.name}). Restart the app for network access to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
