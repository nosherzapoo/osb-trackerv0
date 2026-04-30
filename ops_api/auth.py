"""Auth — bcrypt password + HS256 JWT.

Single admin user. Username + bcrypt'd password hash configured via env:
    ADMIN_USERNAME, ADMIN_PASSWORD_HASH, JWT_SECRET, JWT_TTL_HOURS (default 24)

Generate a hash with `python -m ops_api.hash_password <plaintext>`.
"""

import os
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

ALGORITHM = "HS256"
DEFAULT_TTL_HOURS = 24

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/ops/auth/login", auto_error=False)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        # bcrypt rejects passwords longer than 72 bytes — truncate to match how
        # we hash on the way in (sha256-prehash would be safer but breaks
        # interop; we just clip).
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except Exception:
        return False


def authenticate(username: str, password: str) -> bool:
    expected_user = os.environ.get("ADMIN_USERNAME")
    expected_hash = os.environ.get("ADMIN_PASSWORD_HASH")
    if not expected_user or not expected_hash:
        raise HTTPException(
            status_code=500,
            detail="ops API auth is not configured (ADMIN_USERNAME / ADMIN_PASSWORD_HASH missing)",
        )
    if username != expected_user:
        return False
    return verify_password(password, expected_hash)


def issue_token(username: str) -> tuple[str, int]:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="JWT_SECRET not set")
    ttl_hours = int(os.environ.get("JWT_TTL_HOURS", DEFAULT_TTL_HOURS))
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    payload = {
        "sub": username,
        "exp": int(expires_at.timestamp()),
        "iat": int(datetime.now(timezone.utc).timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm=ALGORITHM)
    return token, ttl_hours * 3600


def require_user(token: str | None = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="JWT_SECRET not set")
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
        return payload["sub"]
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )
