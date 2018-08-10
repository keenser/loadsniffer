#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#

import asyncio
import logging
import aiohttp
import xmltodict
import functools
from . import notify
from . import ssdp
from . import dlna
from . import events


class ResponseHandler(aiohttp.client_proto.ResponseHandler):
    def connection_made(self, transport):
        super().connection_made(transport)
        self.localhost = transport.get_extra_info('sockname')


class TCPConnector(aiohttp.connector.TCPConnector):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._factory = functools.partial(ResponseHandler, loop=self._loop)


class UPNPDevice(object):
    def __init__(self, description={}, parent=None):
        self.description = description
        self.parent = parent
        self.childs = []
        self.services = {}
        for service in self.description.get('serviceList', {}).get('service', []):
            serviceType = self.getName(service.get('serviceType'))
            if serviceType == 'AVTransport':
                dlnaservice = dlna.AVTransport(self.location, service, self)
            else:
                dlnaservice = dlna.DLNAService(self.location, service, self)
            self.services[serviceType] = dlnaservice

        if 'deviceList' in self.description:
            for child in self.description['deviceList'].get('device', []):
                dev = UPNPDevice(description=child, parent=self)
                self.childs.append(dev)
        notify.send('UPnP.Device.detection_completed', device=self)

    @staticmethod
    def getName(typename):
        try:
            return typename.split(':')[-2]
        except IndexError:
            return typename

    @property
    def ssdp(self):
        return self.parent.ssdp

    @property
    def usn(self):
        return self.ssdp.get('usn')

    @property
    def location(self):
        return self.ssdp.get('location')

    @property
    def deviceType(self):
        return self.description.get('deviceType')

    @property
    def friendlyDeviceType(self):
        return self.getName(self.deviceType)

    @property
    def friendlyName(self):
        return self.description.get('friendlyName')

    def service(self, name):
        return self.services.get(name)

    @property
    def events(self):
        return self.parent.events


class UPNPRootDevice(UPNPDevice):
    def __init__(self, description, ssdp, events):
        self._ssdp = ssdp
        self._events = events
        super().__init__(description=description)

    @property
    def ssdp(self):
        return self._ssdp

    @property
    def events(self):
        return self._events


class UPNPServer:
    def __init__(self, loop=None, http=None, httpport=0):
        self.log = logging.getLogger(self.__class__.__name__)
        self.loop = asyncio.get_event_loop()  if loop is None else loop
        self.http = aiohttp.web.Application() if http is None else http
        self.httpport = httpport
        self.handler = None
        self.httpserver = None
        self.devices = {}

        notify.connect('UPnP.SSDP.new_device', self.create_device)
        notify.connect('UPnP.SSDP.removed_device', self.remove_device)
        #notify.connect('UPnP.Device.detection_completed', self.detection_complited)

        self.events = events.EventsServer(loop=self.loop, http=self.http)
        self.ssdp = self.loop.run_until_complete(ssdp.SSDPServer(loop=self.loop))

        if http is None:
            self.handler = self.http.make_handler()
            self.httpserver = self.loop.run_until_complete(self.loop.create_server(self.handler, '0.0.0.0', httpport))
            self.httpport = self.httpserver.sockets[0].getsockname()[1]

    def shutdown(self):
        self.ssdp.shutdown()

        if self.httpserver:
            self.httpserver.close()
            self.loop.run_until_complete(self.httpserver.wait_closed())

        if self.handler:
            self.loop.run_until_complete(self.http.shutdown())
            self.loop.run_until_complete(self.handler.shutdown(60.0))
            self.loop.run_until_complete(self.http.cleanup())


    async def parse_description(self, url):
        try:
            async with aiohttp.ClientSession(connector=TCPConnector(loop=self.loop), read_timeout=5) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise aiohttp.errors.ClientResponseError('Error %d' % resp.status)
                    text = await resp.text()
                    xml = xmltodict.parse(text, dict_constructor=dict, force_list=('device', 'service'))
                    device = xml['root']['device'][0]
                    device['localhost'] = 'http://{}:{}/'.format(resp._protocol.localhost[0], self.httpport)
                    return device
        except (OSError, asyncio.TimeoutError, aiohttp.client_exceptions.ClientError) as err:
            self.log.warn('%s: %s', err.__class__.__name__, err)
            return
 
    async def create_device(self, device=None):
        self.log.warn('create %s', device)
        description = await self.parse_description(device.get('location'))
        if description:
            self.devices[device.get('location')] = UPNPRootDevice(description, device, self.events)

    async def remove_device(self, device=None):
        self.log.warn('remove %s', device)
        if device.get('location') in self.devices:
            notify.send('UPnP.RootDevice.removed', device=self.devices.pop(device.get('location')))

#    async def detection_complited(self, device=None):
#        #print('detection_complited', device.deviceType, device.location)
#        #print('service', device.services)
#        service = device.service('AVTransport')
#        def state_change(data):
#            print('state_change', data)
#
#        if service:
#            await service.subscribe('CurrentTrackMetaData', state_change)
#            await service.subscribe('TransportState', state_change)
#        #    print(await service.stop())
#        #    print(await service.transporturi('http://clips.vorwaerts-gmbh.de/big_buck_bunny.mp4', 'BBB', 'mp4/video'))
#        #    print(await service.play())
