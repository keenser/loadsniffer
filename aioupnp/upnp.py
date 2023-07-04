#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#

from __future__ import annotations
import asyncio
import logging
import functools
import lxml.etree as xml
import aiohttp
import aiohttp.web
import aiohttp.client_proto
import aiohttp.client_exceptions
import aiohttp.connector
from . import notify
from . import ssdp
from . import dlna
from . import events
from typing import Dict, Optional
from functools import cached_property


class ResponseHandler(aiohttp.client_proto.ResponseHandler):
    def connection_made(self, transport):
        super().connection_made(transport)
        self.localhost = transport.get_extra_info('sockname')


class TCPConnector(aiohttp.connector.TCPConnector):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._factory = functools.partial(ResponseHandler, loop=self._loop)


class UPNPDevice:
    def __init__(self, description:dlna.Element, parent:UPNPDevice):
        self.log = logging.getLogger('{}.{}'.format(__name__, self.__class__.__name__))
        self._description = description
        self._parent_device = parent
        self._device_list = []
        self._service_list = {}
        for service in self._description.iterfind('serviceList/service'):
            servicetype = self.getName(service.find('serviceType').text)
            if servicetype == 'AVTransport':
                dlnaservice = dlna.AVTransport(self, service)
            else:
                dlnaservice = dlna.DLNAService(self, service)
            self._service_list[servicetype] = dlnaservice

        for child in self._description.iterfind('deviceList/device'):
            dev = UPNPDevice(description=child, parent=self)
            self._device_list.append(dev)
        notify.send('UPnP.Device.detection_completed', device=self)

    async def shutdown(self):
        self.log.info('shutdown %s %s', self.friendlyName, self.usn)
        for service in self._service_list.values():
            await service.shutdown()

        for device in self._device_list:
            await device.shutdown()

    async def update_callback(self):
        for service in self._service_list.values():
            await service.resubscribe()

    @staticmethod
    def getName(typename):
        try:
            return typename.split(':')[-2]
        except IndexError:
            return typename

    @property
    def root(self):
        return self._parent_device

    @property
    def ssdp(self):
        return self.root.ssdp

    @property
    def usn(self):
        return self.ssdp.get('usn')

    @property
    def location(self):
        return self.ssdp.get('location')

    @cached_property
    def deviceType(self):
        return self._description.get('deviceType')

    @cached_property
    def friendlyDeviceType(self):
        return self.getName(self.deviceType)

    @cached_property
    def friendlyName(self):
        return self._description.get('friendlyName')

    def service(self, name):
        return self._service_list.get(name)

    @property
    def events(self):
        return self.root.events

    @cached_property
    def localhost(self):
        return self.root._description.get('localhost')


class UPNPRootDevice(UPNPDevice):
    def __init__(self, description, ssdp, events):
        self._ssdp = ssdp
        self._events = events
        super().__init__(description=description, parent=self)

    # @property
    # def root(self):
    #     return self

    @property
    def ssdp(self):
        return self._ssdp

    @property
    def events(self):
        return self._events


class UPNPServer(ssdp.SSDPServer):
    def __init__(self,
                 loop: Optional[asyncio.AbstractEventLoop] = None,
                 http: Optional[aiohttp.web.Application] = None,
                 httpport: int = 0
                 ) -> None:
        super().__init__()

        self.log = logging.getLogger('{}.{}'.format(__name__, __class__.__name__))
        self.loop = loop or asyncio.get_event_loop()
        self.http = aiohttp.web.Application() if http is None else http #can't use 'http or Application()'
        self.httpport = httpport
        self.handler = None
        self.httpserver = None
        self.devices: Dict[str, UPNPRootDevice] = {}

        self.events = events.EventsServer(loop=self.loop, http=self.http)

        self.http.on_shutdown.append(self.shutdown)

        if http is None:
            self.handler = self.http.make_handler()
            self.httpserver = self.loop.run_until_complete(self.loop.create_server(self.handler, '0.0.0.0', httpport))
            self.httpport = self.httpserver.sockets[0].getsockname()[1]

    async def shutdown(self, app=None):
        #for device in self.devices.values():
        #    await device.shutdown()

        await super().shutdown()

        if self.httpserver:
            self.httpserver.close()
            await self.httpserver.wait_closed()

        if self.handler:
            await self.http.shutdown()
            await self.handler.shutdown(60.0)
            await self.http.cleanup()

    async def parse_description(self, url: str) -> Optional[dlna.Element]:
        try:
            async with aiohttp.ClientSession(connector=TCPConnector(loop=self.loop), read_timeout=5, raise_for_status=True) as session:
                async with session.get(url) as resp:
                    data = await resp.read()
                    spec = dlna.didl.fromString(data)
                    device = spec.find('device')
                    _localhost = xml.SubElement(device, xml.QName(device.nsmap[None], 'localhost'), attrib=None, nsmap=None)
                    _localhost.text = 'http://{}:{}/'.format(resp._protocol.localhost[0], self.httpport)
                    return device
        except (OSError, asyncio.TimeoutError, aiohttp.client_exceptions.ClientError) as err:
            self.log.warning('%s: %s', err.__class__.__name__, err)
            return

    async def device_created(self, device=None):
        self.log.warning('create %s', device)
        description = await self.parse_description(device['location'])
        if description is not None:
            self.devices[device['usn']] = UPNPRootDevice(description, device, self.events)

    async def device_removed(self, device=None):
        self.log.warning('remove %s', device)
        if device.get('usn') in self.devices:
            upnpdevice = self.devices.pop(device['usn'])
            notify.send('UPnP.RootDevice.removed', device=upnpdevice)
            await upnpdevice.shutdown()

    async def device_updated(self, device=None):
        self.log.warning('update %s', device)
        if device.get('usn') in self.devices:
            upnpdevice = self.devices[device['usn']]
            upnpdevice._ssdp = device
            await upnpdevice.update_callback()
        else:
            await self.device_created(device=device)
