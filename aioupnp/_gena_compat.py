"""Compatibility shim for async_upnp_client's GENA resubscribe handling.

``UpnpEventHandler._async_do_resubscribe`` (async_upnp_client.event_handler)
trusts whatever SID a device echoes back on subscription *renewal*: if it
differs from the SID we sent, the old SID->service mapping is dropped and
the new one takes over::

    if "sid" in response.headers and response.headers["sid"]:
        new_sid = response.headers["sid"]
        if new_sid != sid:
            del self._subscriptions[sid]
            sid = new_sid

Some real devices (LG WebOS TVs observed in practice) echo back a SID on
renewal that doesn't match what they actually keep sending NOTIFYs under.
Trusting it desyncs ``UpnpEventHandler``'s SID->service map from the SID the
device is really using: every subsequent NOTIFY for the (still perfectly
valid, from the device's point of view) original SID has no matching entry,
so it is parked in ``UpnpEventHandler``'s unmatched-SID backlog forever and
nothing ever calls ``on_event`` again - even though ``DmrDevice.is_subscribed``
keeps reporting True.

This project's old, working ``aioupnp`` implementation (see git history,
``aioupnp/events.py`` before the async-upnp-client migration) renewed
subscriptions the same way (``SUBSCRIBE`` with a ``SID`` header) but never
looked at the SID in the renewal response at all - it just kept using the
SID from the original subscribe. This shim restores that behaviour: renewal
always keeps tracking events under the SID we already have, ignoring
whatever the device echoes back.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlparse

import async_upnp_client.event_handler as _event_handler
from async_upnp_client.const import HttpRequest
from async_upnp_client.exceptions import UpnpResponseError

_LOGGER = logging.getLogger(__name__)

# Marker used to keep the patch idempotent.
_PATCHED = "_gena_compat_patched"


async def _resubscribe_keep_sid(self, service, sid, timeout=timedelta(seconds=1800)):
    """Renew a subscription without ever switching to a device-echoed SID."""
    headers = {
        "HOST": urlparse(service.event_sub_url).netloc,
        "SID": sid,
        "TIMEOUT": "Second-" + str(timeout.total_seconds()),
    }
    request = HttpRequest("SUBSCRIBE", service.event_sub_url, headers, None)
    response = await self._requester.async_http_request(request)

    if response.status_code != 200:
        _LOGGER.debug("Did not receive 200, but %s", response.status_code)
        raise UpnpResponseError(status=response.status_code, headers=response.headers)

    # Deliberately not looking at response.headers["sid"] here - see module
    # docstring.
    if (
        "timeout" in response.headers
        and response.headers["timeout"] != "Second-infinite"
        and "Second-" in response.headers["timeout"]
    ):
        response_timeout = response.headers["timeout"]
        timeout_seconds = int(response_timeout[7:])  # len("Second-") == 7
        timeout = timedelta(seconds=timeout_seconds)

    self._subscriptions[sid] = service
    _LOGGER.debug("Resubscribed (SID kept as-is), service: %s, SID: %s, timeout: %s", service, sid, timeout)

    return sid, timeout


def apply() -> None:
    """Install the compatibility shim for ``_async_do_resubscribe``.

    Idempotent: patching twice is a no-op. Safe to call from ``__init__.py``
    at import time, before any subscription is made.
    """
    if getattr(_event_handler.UpnpEventHandler._async_do_resubscribe, _PATCHED, False):
        return

    setattr(_resubscribe_keep_sid, _PATCHED, True)
    _event_handler.UpnpEventHandler._async_do_resubscribe = _resubscribe_keep_sid
    _LOGGER.debug("async_upnp_client GENA resubscribe patched (SID kept as-is)")
