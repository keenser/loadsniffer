#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
"""DLNA MediaRenderer control point, built on async-upnp-client."""

from __future__ import annotations
import asyncio
import logging
from typing import Awaitable, Callable, Dict, Optional, Set, Tuple

from async_upnp_client.aiohttp import AiohttpNotifyServer, AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.event_handler import UpnpEventHandlerRegister
from async_upnp_client.exceptions import UpnpError
from async_upnp_client.profiles.dlna import DmrDevice
from async_upnp_client.search import async_search
from async_upnp_client.ssdp import SSDP_MX, SSDP_ST_ROOTDEVICE, udn_from_usn
from async_upnp_client.utils import CaseInsensitiveDict

RESEARCH_INTERVAL = 30
MISSED_ROUNDS_BEFORE_REMOVE = 2

DeviceCallback = Callable[[DmrDevice], Awaitable[None]]


class RendererRegistry:
    """Discovers DLNA MediaRenderers on the LAN and hands out DmrDevice profiles for them.

    Deliberately uses only *active* M-SEARCH polling (async_search), not a persistent
    NOTIFY listener: a persistent listener would bind another SO_REUSEPORT socket to
    0.0.0.0:1900, and on Linux that port is load-balanced by a 4-tuple hash across all
    SO_REUSEPORT sockets bound to it - so a given remote host's M-SEARCH packets can end
    up delivered only to this listener (which ignores M-SEARCH) instead of to
    MediaServer's SsdpSearchResponder, which is the one that's supposed to answer them.
    """

    def __init__(self,
                 loop: Optional[asyncio.AbstractEventLoop] = None,
                 on_device_found: Optional[DeviceCallback] = None,
                 on_device_removed: Optional[DeviceCallback] = None,
                 source: Optional[Tuple[str, int]] = None
                 ) -> None:
        self.log = logging.getLogger('{}.{}'.format(__name__, self.__class__.__name__))
        self.loop = loop or asyncio.get_event_loop()
        self._on_device_found = on_device_found
        self._on_device_removed = on_device_removed
        self._source = source

        self._requester = AiohttpRequester()
        self._factory = UpnpFactory(self._requester)
        self._event_handlers = UpnpEventHandlerRegister(self._requester, AiohttpNotifyServer)
        self._devices: Dict[str, DmrDevice] = {}
        self._missed_rounds: Dict[str, int] = {}
        self._research_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._research_task = self.loop.create_task(self._research())

    async def stop(self) -> None:
        if self._research_task:
            self._research_task.cancel()
            try:
                await self._research_task
            except asyncio.CancelledError:
                pass
        for udn in list(self._devices):
            await self._remove_device(udn)

    async def async_search(self) -> None:
        await self._search_round()

    async def _research(self) -> None:
        while True:
            await self._search_round()
            await asyncio.sleep(RESEARCH_INTERVAL)

    async def _search_round(self) -> None:
        seen: Set[str] = set()

        async def on_response(headers: CaseInsensitiveDict) -> None:
            await self._on_search_response(headers, seen)

        try:
            await async_search(
                async_callback=on_response,
                timeout=SSDP_MX,
                search_target=SSDP_ST_ROOTDEVICE,
                source=self._source,
                loop=self.loop,
            )
        except UpnpError as err:
            self.log.warning('search round failed: %s', err)
            return

        await self._reap(seen)

    async def _reap(self, seen: Set[str]) -> None:
        for udn in list(self._devices):
            if udn in seen:
                self._missed_rounds.pop(udn, None)
                continue
            missed = self._missed_rounds.get(udn, 0) + 1
            if missed >= MISSED_ROUNDS_BEFORE_REMOVE:
                self._missed_rounds.pop(udn, None)
                await self._remove_device(udn)
            else:
                self._missed_rounds[udn] = missed

    async def _on_search_response(self, headers: CaseInsensitiveDict, seen: Set[str]) -> None:
        usn = headers.get_lower('usn')
        udn = usn and udn_from_usn(usn)
        if not udn:
            return
        seen.add(udn)

        if udn in self._devices:
            return

        location = headers.get_lower('location')
        if not location:
            return

        try:
            device = await self._factory.async_create_device(location)
        except (UpnpError, OSError, asyncio.TimeoutError) as err:
            self.log.warning('failed to fetch device description from %s: %s', location, err)
            return

        if not DmrDevice.is_profile_device(device):
            return

        self.log.info('found media renderer %s (%s)', device.name, device.udn)
        event_handler = await self._event_handlers.async_add_device(device)
        dmr = DmrDevice(device, event_handler)
        try:
            await dmr.async_subscribe_services(auto_resubscribe=True)
        except UpnpError as err:
            self.log.warning('failed to subscribe to events of %s: %s', device.name, err)

        self._devices[device.udn] = dmr
        if self._on_device_found:
            await self._on_device_found(dmr)

    async def _remove_device(self, udn: str) -> None:
        dmr = self._devices.pop(udn, None)
        if not dmr:
            return

        self.log.info('removed media renderer %s (%s)', dmr.name, udn)
        try:
            await dmr.async_unsubscribe_services()
        except UpnpError:
            pass
        await self._event_handlers.async_remove_device(dmr.device)
        if self._on_device_removed:
            await self._on_device_removed(dmr)
