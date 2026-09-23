#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
"""DLNA MediaRenderer control point, built on async-upnp-client."""

from __future__ import annotations
import asyncio
import logging
from typing import Awaitable, Callable, Dict, Optional, Tuple

from async_upnp_client.aiohttp import AiohttpNotifyServer, AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.const import SsdpSource
from async_upnp_client.event_handler import UpnpEventHandlerRegister
from async_upnp_client.exceptions import UpnpError
from async_upnp_client.profiles.dlna import DmrDevice
from async_upnp_client.ssdp import SSDP_ST_ROOTDEVICE
from async_upnp_client.ssdp_listener import SsdpListener

RESEARCH_INTERVAL = 120

DeviceCallback = Callable[[DmrDevice], Awaitable[None]]


class RendererRegistry:
    """Discovers DLNA MediaRenderers on the LAN and hands out DmrDevice profiles for them.

    Uses SsdpListener (passive NOTIFY alive/byebye + active M-SEARCH), not just active
    search: some real devices (e.g. certain LG WebOS TVs) inconsistently omit LOCATION
    from individual M-SEARCH responses while including it reliably in their NOTIFY
    alive announcements. SsdpListener's SsdpDevice also accumulates location from
    *any* packet seen for a UDN rather than treating each packet in isolation, so a
    single good packet (from either source) is enough to make the device usable.
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

        self._requester = AiohttpRequester()
        self._factory = UpnpFactory(self._requester)
        self._event_handlers = UpnpEventHandlerRegister(self._requester, AiohttpNotifyServer)
        self._devices: Dict[str, DmrDevice] = {}
        self._pending: set = set()
        self._listener = SsdpListener(
            async_callback=self._on_ssdp,
            loop=self.loop,
            search_target=SSDP_ST_ROOTDEVICE,
            source=source,
        )
        self._research_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        await self._listener.async_start()
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
        await self._listener.async_stop()

    async def async_search(self) -> None:
        await self._listener.async_search()

    async def _research(self) -> None:
        while True:
            await self.async_search()
            await asyncio.sleep(RESEARCH_INTERVAL)

    async def _on_ssdp(self, ssdp_device, device_or_service_type: str, source: SsdpSource) -> None:
        if device_or_service_type != SSDP_ST_ROOTDEVICE:
            return

        if source == SsdpSource.ADVERTISEMENT_BYEBYE:
            await self._remove_device(ssdp_device.udn)
            return

        if source == SsdpSource.ADVERTISEMENT_UPDATE and ssdp_device.udn in self._devices:
            await self._remove_device(ssdp_device.udn)

        # Guard against a second SSDP packet for the same not-yet-tracked UDN
        # arriving (and being handled concurrently) while we're still in the
        # middle of fetching the description and subscribing for the first
        # one - e.g. a search response and an alive NOTIFY for the same
        # device landing within the same event loop iteration. Without this,
        # both would race past the "in self._devices" check below and each
        # end up with their own, independent SUBSCRIBE to the same physical
        # service, which is unnecessary and can confuse devices that don't
        # expect two live subscriptions from the same callback URL.
        if ssdp_device.udn in self._devices or ssdp_device.udn in self._pending:
            return

        location = ssdp_device.location
        if not location:
            self.log.debug('%s seen but no LOCATION known yet (will retry on next packet)', ssdp_device.udn)
            return

        self._pending.add(ssdp_device.udn)
        try:
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
        finally:
            self._pending.discard(ssdp_device.udn)

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
