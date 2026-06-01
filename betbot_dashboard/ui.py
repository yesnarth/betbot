"""Small UI helpers shared across dashboard sections."""
from __future__ import annotations

import functools

import streamlit as st

from betbot_dashboard.api_client import ApiError


def guarded(fn):
    """Wrap a section renderer so a backend `ApiError` renders as a friendly
    message inside that tab instead of crashing the whole page with a raw
    Streamlit traceback. The rest of the dashboard keeps working.
    """
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ApiError as exc:
            st.error(f"⚠️ {exc.user_message}")
            if exc.status_code:
                st.caption(f"Code HTTP {exc.status_code}")
            return None
    return _wrapper
