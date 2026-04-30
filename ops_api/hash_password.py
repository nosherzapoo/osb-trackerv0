"""Generate a bcrypt hash for the admin password.

Usage:
    python -m ops_api.hash_password '<plaintext password>'
"""

import sys

import bcrypt


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m ops_api.hash_password '<plaintext password>'")
        sys.exit(2)
    secret = sys.argv[1].encode("utf-8")[:72]
    hashed = bcrypt.hashpw(secret, bcrypt.gensalt(rounds=12))
    print(hashed.decode("utf-8"))


if __name__ == "__main__":
    main()
