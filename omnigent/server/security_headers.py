"""Browser hardening headers added to every HTTP response.

``X-Content-Type-Options`` and ``Referrer-Policy`` are always sent. HSTS is
sent when the deployment's public URL is HTTPS. Framing is left open unless
``OMNIGENT_FRAME_ANCESTORS`` is set, because some deployments embed the web
UI in another origin (an editor webview, a workspace app).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from starlette.datastructures import MutableHeaders

FRAME_ANCESTORS_ENV = "OMNIGENT_FRAME_ANCESTORS"
_HSTS_VALUE = "max-age=31536000"
# One CSP source list: keywords, schemes and hosts, no directive separators.
_SOURCE_LIST_RE = re.compile(r"^[A-Za-z0-9'*:/.\-\[\] ]+$")


def build_security_headers(
    *, https: bool, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Headers to set on responses that do not already carry them.

    :param https: Whether the deployment is served over HTTPS, e.g. its
        public base URL starts with ``https://``.
    :param environ: Environment to read ``OMNIGENT_FRAME_ANCESTORS`` from,
        e.g. ``{"OMNIGENT_FRAME_ANCESTORS": "'self'"}``.
    :raises ValueError: When the frame-ancestors value is not a plain CSP
        source list.
    """
    env = environ if environ is not None else os.environ
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
    if https:
        headers["Strict-Transport-Security"] = _HSTS_VALUE
    frame_ancestors = env.get(FRAME_ANCESTORS_ENV, "").strip()
    if frame_ancestors:
        if not _SOURCE_LIST_RE.fullmatch(frame_ancestors):
            raise ValueError(
                f"{FRAME_ANCESTORS_ENV} must be a CSP source list such as 'self' "
                f"or 'none'; got {frame_ancestors!r}"
            )
        headers["Content-Security-Policy"] = f"frame-ancestors {frame_ancestors}"
        if frame_ancestors == "'none'":
            headers["X-Frame-Options"] = "DENY"
        elif frame_ancestors == "'self'":
            headers["X-Frame-Options"] = "SAMEORIGIN"
    return headers


def apply_security_headers(response_headers: MutableHeaders, headers: Mapping[str, str]) -> None:
    """Add each header unless the route already set its own value."""
    for name, value in headers.items():
        if name not in response_headers:
            response_headers[name] = value
