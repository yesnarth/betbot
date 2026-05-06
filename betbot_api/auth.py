"""
HTTP Basic auth dependency.

Disabled when API_BASIC_PASSWORD is empty (dev convenience). In production
set API_BASIC_USER and API_BASIC_PASSWORD in .env.
"""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from betbot.config import load_settings

_security = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_security)) -> str:
    """
    Returns the authenticated username (or "anonymous" when auth is disabled).
    Raises 401 on bad credentials.
    """
    s = load_settings()
    if not s.api_basic_password:
        return "anonymous"  # auth disabled — fine in dev/local

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    user_ok = secrets.compare_digest(credentials.username, s.api_basic_user)
    pass_ok = secrets.compare_digest(credentials.password, s.api_basic_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bad credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
