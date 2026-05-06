"""
JWT authentication — replaces the legacy HTTP Basic in production.

Backward-compatible behavior:
  - When BETBOT_JWT_SECRET is empty, JWT is disabled and the legacy HTTP Basic
    middleware (require_auth) keeps working. This avoids breaking existing
    dashboards that haven't migrated yet.
  - When BETBOT_JWT_SECRET is set, /auth/login issues a token and protected
    endpoints accept either JWT (Authorization: Bearer …) OR HTTP Basic
    (transition mode).

User credentials live in two simple env vars (single-user setup):
  - BETBOT_USERNAME (default "betbot")
  - BETBOT_PASSWORD_HASH  (bcrypt hash; generate with passlib.context)

Generate a hash:
    python -c "from passlib.context import CryptContext; \
        print(CryptContext(schemes=['bcrypt']).hash('your_password'))"
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials, OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from betbot.config import load_settings


JWT_ALGO = "HS256"
JWT_ACCESS_TOKEN_TTL_MIN = int(os.getenv("BETBOT_JWT_TTL_MIN", "60"))   # 1 hour default

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
_oauth_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)
_basic_scheme = HTTPBasic(auto_error=False)


def jwt_enabled() -> bool:
    return bool(os.getenv("BETBOT_JWT_SECRET", "").strip())


def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_ctx.verify(plain, hashed)
    except (ValueError, TypeError):
        return False


def authenticate_user(username: str, password: str) -> bool:
    """Validate username + password against the configured credentials."""
    expected_user = os.getenv("BETBOT_USERNAME", "betbot")
    if username != expected_user:
        return False
    pwd_hash = os.getenv("BETBOT_PASSWORD_HASH", "")
    if pwd_hash:
        return _verify_password(password, pwd_hash)
    # Fallback: plain BETBOT_PASSWORD (less safe — only for early setup)
    plain = os.getenv("BETBOT_PASSWORD", "")
    return bool(plain) and password == plain


def create_access_token(subject: str) -> str:
    secret = os.getenv("BETBOT_JWT_SECRET", "")
    if not secret:
        raise RuntimeError("BETBOT_JWT_SECRET not set — cannot issue tokens")
    expire = datetime.now(timezone.utc) + timedelta(minutes=JWT_ACCESS_TOKEN_TTL_MIN)
    payload = {"sub": subject, "exp": expire, "iat": datetime.now(timezone.utc)}
    return jwt.encode(payload, secret, algorithm=JWT_ALGO)


def decode_token(token: str) -> dict | None:
    """Return the JWT payload if valid, else None."""
    secret = os.getenv("BETBOT_JWT_SECRET", "")
    if not secret:
        return None
    try:
        return jwt.decode(token, secret, algorithms=[JWT_ALGO])
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# Dependency: try JWT first, then HTTP Basic (transition mode)
# ---------------------------------------------------------------------------

def require_auth_combined(
    bearer: Optional[str] = Depends(_oauth_scheme),
    basic: Optional[HTTPBasicCredentials] = Depends(_basic_scheme),
) -> str:
    """
    Resolve the authenticated user against BOTH auth schemes:
      1. JWT bearer (preferred when BETBOT_JWT_SECRET is set)
      2. HTTP Basic fallback (legacy)
      3. Anonymous when nothing is configured (dev / local mode)
    """
    s = load_settings()

    # Try JWT first
    if bearer and jwt_enabled():
        payload = decode_token(bearer)
        if payload:
            return payload.get("sub", "anonymous")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired JWT",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # HTTP Basic fallback
    if s.api_basic_password:
        if basic is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
        import secrets as _secrets
        if (_secrets.compare_digest(basic.username, s.api_basic_user) and
                _secrets.compare_digest(basic.password, s.api_basic_password)):
            return basic.username
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bad credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    # No auth configured → dev mode
    return "anonymous"
