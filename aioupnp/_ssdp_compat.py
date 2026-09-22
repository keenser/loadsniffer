"""Compatibility shim for async_upnp_client's SSDP header parsing.

async_upnp_client 0.48.x (``async_upnp_client.ssdp._cached_header_parse``)
feeds ``aiohttp.http_parser.HeadersParser.parse_headers`` a list of header
lines *already stripped of the request/status line* (``lines[1:]``).

The aiohttp API for ``parse_headers`` changed across versions:

* aiohttp < 3.12 starts parsing at ``lines[1]``, so the first element of the
  list must be the request/status line. Given a list that starts with a real
  header, the first header (almost always the SSDP ``Location`` line) is
  silently dropped.
* aiohttp >= 3.12 starts parsing at ``lines[0]`` and expects header lines
  only.

The result is that on aiohttp 3.11.16 (as installed on the deployment server)
the ``Location`` header of an SSDP response is lost, which makes
``SsdpDeviceTracker.see_search`` reject the packet so the device never
appears.

Instead of depending on the installed aiohttp version, this module replaces
``_cached_header_parse`` with a small self-contained parser that does not use
aiohttp's ``HeadersParser`` at all (mirroring the custom implementation that
used to live in the old ``aioupnp`` code). It behaves identically on every
aiohttp version.
"""

from __future__ import annotations

import functools
import logging

import async_upnp_client.ssdp as _ssdp

_LOGGER = logging.getLogger(__name__)

# Marker used to keep the patch idempotent.
_PATCHED = "_ssdp_compat_patched"


def _manual_header_parse(data: bytes) -> tuple[dict[str, str], str, object]:
    """Parse an SSDP packet into (headers, request_line, udn).

    ``data`` is the raw packet (bytes). Mirrors the behaviour async_upnp_client
    needs from ``_cached_header_parse``: the first line is the request/status
    line, every following ``name: value`` line is a header (lower-cased keys,
    value stripped of surrounding whitespace), and the UDN is derived from the
    ``USN`` header.
    """
    lines = data.replace(b"\r\n", b"\n").split(b"\n")
    request_line = lines[0].strip().decode() if lines else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        try:
            key, value = line.split(b":", 1)
        except ValueError:
            # Line without a colon (e.g. a stray empty/malformed line).
            continue
        key = key.strip().decode()
        if not key:
            continue
        headers[key.lower()] = value.strip().decode()

    usn = headers.get("usn")
    udn = _ssdp.udn_from_usn(usn) if usn else None
    return headers, request_line, udn


def apply() -> None:
    """Install the compatibility shim for ``_cached_header_parse``.

    Idempotent: patching twice is a no-op. Safe to call from ``__init__.py``
    at import time, before any ``SsdpListener`` is created.
    """
    if getattr(_ssdp._cached_header_parse, _PATCHED, False):
        return

    patched = functools.lru_cache(maxsize=128)(_manual_header_parse)
    setattr(patched, _PATCHED, True)
    _ssdp._cached_header_parse = patched
    _LOGGER.debug("async_upnp_client SSDP header parsing patched (manual parser)")