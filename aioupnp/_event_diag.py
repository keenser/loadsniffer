"""TEMPORARY diagnostics for the "GENA NOTIFY matched but dmr.on_event never
fires" investigation.

We've confirmed (via async_upnp_client.traffic.upnp debug logs) that NOTIFYs
are reaching UpnpEventHandler.handle_notify and are being matched to a
subscribed service (no "Storing NOTIFY in backlog" messages), yet
mrc.py's UPnPctrl._on_dmr_event never runs (its own INFO log line never
appears). The remaining link in the chain is:

    UpnpService.notify_changed_state_variables(changes)
        -> if self.on_event: self.on_event(self, changed_state_variables)
           (DmrDevice._on_event, if bound correctly)
        -> DmrDevice.on_event(service, state_variables)
           (UPnPctrl._on_dmr_event, via functools.partial)

This wraps (does NOT replace) notify_changed_state_variables to log, for
every NOTIFY that reaches it, the service id, what changed, and whether
on_event is actually bound - so we can see directly which of those two
callbacks is missing instead of guessing. Remove this module (and its
import/apply() call in aioupnp/__init__.py) once the root cause is found.
"""

from __future__ import annotations

import logging

import async_upnp_client.client as _client

_LOGGER = logging.getLogger(__name__)

_PATCHED = "_event_diag_patched"


def apply() -> None:
    if getattr(_client.UpnpService.notify_changed_state_variables, _PATCHED, False):
        return

    original = _client.UpnpService.notify_changed_state_variables

    def _notify_with_log(self, changes):
        _LOGGER.info(
            'DIAG notify_changed_state_variables service=%s changes=%s on_event=%r',
            self.service_id,
            dict(changes),
            self.on_event,
        )
        return original(self, changes)

    setattr(_notify_with_log, _PATCHED, True)
    _client.UpnpService.notify_changed_state_variables = _notify_with_log
    _LOGGER.debug("event diagnostics installed on UpnpService.notify_changed_state_variables")
