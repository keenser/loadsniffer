#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
"""Renderer control point: tracks discovered DLNA MediaRenderers and casts to the active one."""

from __future__ import annotations
import asyncio
import functools
import logging
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

import aiohttp.web
from pydantic import BaseModel

import aioupnp
from async_upnp_client.client import UpnpService, UpnpStateVariable
from async_upnp_client.exceptions import UpnpError
from async_upnp_client.profiles.dlna import DmrDevice


class RendererInfo(BaseModel):
    udn: str
    name: str


class PlaybackItem(BaseModel):
    url: Optional[str] = None
    title: Optional[str] = None


class RendererStatus(BaseModel):
    state: Optional[str] = None
    item: List[PlaybackItem] = []
    device: Optional[str] = None
    udn: str
    devices: List[RendererInfo] = []


@dataclass
class Subscription:
    """One add_alert_handler() registration: the callback plus the last status
    it was sent, so trigger_callbacks() can skip re-sending an unchanged one."""
    callback: Callable[[RendererStatus], Awaitable[None]]
    status: Optional[RendererStatus] = None


class MediaDevice:
    def __init__(self, dmr: DmrDevice):
        self.dmr = dmr
        self.last_url: Optional[str] = None
        self.status = RendererStatus(device=dmr.name, udn=dmr.udn)

    def __repr__(self):
        return "{} {}".format(self.dmr.name, self.status)


class UPnPctrl:
    # Sentinel udn representing "local browser playback" in the device picker
    # (a real DmrDevice udn is always a non-empty "uuid:..." string, so ''
    # can't collide with one).
    LOCAL_UDN = ''

    def __init__(self,
                 loop: Optional[asyncio.AbstractEventLoop] = None,
                 http: Optional[aiohttp.web.Application] = None,
                 httpport: int = 0,
                 source: Optional[tuple] = None
                 ) -> None:
        self.log = logging.getLogger(self.__class__.__name__)

        self.loop = loop or asyncio.get_event_loop()
        self.httpport = httpport
        self.registry = aioupnp.RendererRegistry(
            loop=self.loop,
            on_device_found=self._media_renderer_found,
            on_device_removed=self._media_renderer_removed,
            source=source,
        )

        self.mediadevices: Dict[str, MediaDevice] = {}
        self.device: Optional[MediaDevice] = None
        # Auto-select the first renderer ever found (self.device is None can
        # also mean "user explicitly picked local playback" - only the
        # former should auto-select). select_device() turns this off for
        # good on any explicit choice, including local, so a later renderer
        # never silently overrides it.
        self._auto_select = True
        self.registered_callbacks: Dict[int, Subscription] = {}

    async def start(self) -> None:
        await self.registry.start()

    async def shutdown(self) -> None:
        await self.registry.stop()

    async def _media_renderer_removed(self, dmr: DmrDevice) -> None:
        self.log.info('media renderer removed %s %s', dmr.udn, dmr.name)
        self.mediadevices.pop(dmr.udn, None)
        if self.device and self.device.dmr.udn == dmr.udn:
            self.device = next(iter(self.mediadevices.values()), None)
            self.trigger_callbacks()

    async def _media_renderer_found(self, dmr: DmrDevice) -> None:
        self.log.info('found media renderer %s %s', dmr.udn, dmr.name)

        mediadevice = MediaDevice(dmr)
        self.mediadevices[dmr.udn] = mediadevice
        # Only auto-select the very first renderer ever found - a second one
        # no longer silently steals focus from whatever is already selected,
        # and once the user has made any explicit choice (see select_device),
        # auto-selection never kicks back in.
        if self.device is None and self._auto_select:
            self.device = mediadevice
        self.trigger_callbacks()

        dmr.on_event = functools.partial(self._on_dmr_event, mediadevice)

    def _on_dmr_event(self, mediadevice: MediaDevice, service: UpnpService, state_variables: Sequence[UpnpStateVariable]) -> None:
        dmr = mediadevice.dmr
        state = dmr.transport_state
        mediadevice.status.state = state.name if state else None
        mediadevice.status.item = (
            [PlaybackItem(url=mediadevice.last_url, title=dmr.media_title)] if dmr.media_title else []
        )
        self.log.info('%s now: %s %s', dmr.name, mediadevice.status.state, dmr.media_title)
        self.trigger_callbacks()

    def _local_address_for(self, remote_host: str) -> str:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((remote_host, 1))
            return probe.getsockname()[0]

    async def transporturi(self, url, title='Video', relative=False):
        if self.device:
            dmr = self.device.dmr
            if relative:
                remote_host = urllib.parse.urlparse(dmr.device.device_url).hostname
                local_ip = self._local_address_for(remote_host)
                self.log.debug('local: %s url: %s location: %s', local_ip, url, dmr.device.device_url)
                url = urllib.parse.urljoin('http://{}:{}/'.format(local_ip, self.httpport), url)
            self.device.last_url = url
            try:
                try:
                    await dmr.async_stop()
                except UpnpError:
                    pass
                # DLNA.ORG_OP=01 advertises time-based positioning/seek to the
                # renderer. Without it (the library's default "*") seek/rewind
                # is disabled on many devices (e.g. LG WebOS). Mirrors the old
                # aioupnp setavtransporturi() behaviour.
                meta_data = await dmr.construct_play_media_metadata(
                    url,
                    title,
                    override_dlna_features=(
                        'DLNA.ORG_OP=01;DLNA.ORG_CI=0;'
                        'DLNA.ORG_FLAGS=01700000000000000000000000000000'
                    ),
                )
                await dmr.async_set_transport_uri(url, title, meta_data=meta_data)
                await dmr.async_play()
            except (UpnpError, OSError, asyncio.TimeoutError) as err:
                self.log.warning('transporturi %s', err)
                return

    async def play(self):
        if self.device:
            try:
                await self.device.dmr.async_play()
            except UpnpError as err:
                self.log.warning('play %s', err)

    async def pause(self):
        if self.device:
            try:
                await self.device.dmr.async_pause()
            except UpnpError as err:
                self.log.warning('pause %s', err)

    async def stop(self):
        if self.device:
            try:
                await self.device.dmr.async_stop()
            except UpnpError as err:
                self.log.warning('stop %s', err)

    def add_alert_handler(self, callback):
        self.registered_callbacks[hash(callback)] = Subscription(callback=callback)
        self.trigger_callbacks()

    def remove_alert_handler(self, callback):
        self.registered_callbacks.pop(hash(callback), None)

    @property
    def status(self) -> RendererStatus:
        """Active target's status, plus the picker's list: Local + every known renderer.

        Never None - "no renderer selected" is local browser playback, itself
        a selectable entry (LOCAL_UDN), not the absence of a status.
        """
        devices = [RendererInfo(udn=self.LOCAL_UDN, name='Local')]
        devices += [
            RendererInfo(udn=udn, name=mediadevice.dmr.name) for udn, mediadevice in self.mediadevices.items()
        ]
        if self.device is None:
            status = RendererStatus(udn=self.LOCAL_UDN)
        else:
            status = self.device.status.model_copy()
        status.devices = devices
        return status

    def select_device(self, udn: Optional[str]) -> None:
        """Explicitly switch the active target: a renderer's udn, or LOCAL_UDN for local playback."""
        if udn is None:
            return
        if udn == self.LOCAL_UDN:
            target = None
        else:
            target = self.mediadevices.get(udn)
            if target is None:
                return
        self._auto_select = False
        if target is not self.device:
            self.device = target
            self.trigger_callbacks()

    def trigger_callbacks(self):
        for subscription in self.registered_callbacks.values():
            try:
                status = self.status
                if subscription.status != status:
                    self.loop.create_task(subscription.callback(status))
                    subscription.status = status.model_copy()
            except Exception as exeption:
                self.log.error('trigger_callbacks exception %s', exeption)

    async def refresh(self):
        await self.registry.async_search()
