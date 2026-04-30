"""Generate a bcrypt hash for the admin password.

Usage:
    python -m ops_api.hash_password '<plaintext password>'
"""

import sys

from passlib.context import CryptContext


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m ops_api.hash_password '<plaintext password>'")
        sys.exit(2)
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    hashed = pwd_context.hash(sys.argv[1])
    print(hashed)


if __name__ == "__main__":
    main()
