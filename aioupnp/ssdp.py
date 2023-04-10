#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#

import asyncio
import logging
import socket
import struct
import ipaddress
import urllib.parse
from . import notify
from . import version

SSDP_PORT = 1900
SSDP_ADDR = '239.255.255.250'


class SSDPDevice(dict):
    render_headers = ['nts', 'usn', 'nt', 'location', 'server', 'cache-control']

    def __init__(self, server, data={}):
        super().__init__(data)
        self.log = logging.getLogger('{}.{}'.format(__name__, __class__.__name__))
        self.server = server
        self.handle = None
        self.manifestation = None
        self.silent = False

    def shutdown(self):
        if self.handle:
            self.handle.cancel()

    def __bytes__(self):
        return '\r\n'.join((
            'NOTIFY * HTTP/1.1',
            'HOST: %s:%d' % (SSDP_ADDR, SSDP_PORT))
            +
            tuple('{}: {}'.format(x.upper(), self.get(x)) for x in self.render_headers)
            +
            ('', '')
        ).encode()

    def ssdpalive(self):
        pass


class SSDPRemoteDevice(SSDPDevice):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.manifestation = 'remote'
        self.ssdpalive()

    def ssdpalive(self):
        if self.handle:
            self.handle.cancel()

        _, expiry = self.get('cache-control', 'max-age=60').split('=')
        self.handle = self.server.loop.call_later(int(expiry) + 5, self.server.unregister, self.get('usn'))


class SSDPLocalDevice(SSDPDevice):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.manifestation = 'local'
        self['nts'] = 'ssdp:alive'
        if not self.silent:
            self.handle = self.server.loop.create_task(self._resend_notify())

    def shutdown(self):
        self['nts'] = 'ssdp:byebye'
        if not self.silent:
            self.send_notify()
        super().shutdown()

    def send_notify(self) -> None:
        self.log.info('Sending %s notification for %s', self.get('nts'), self.get('usn'))
        self.log.debug('send_notify content %s', self)
        try:
            self.server.transport.sendto(bytes(self), (SSDP_ADDR, SSDP_PORT))
        except (AttributeError, socket.error) as msg:
            self.log.info("failure sending out alive notification: %r", msg)

    async def _resend_notify(self):
        while True:
            self.send_notify()
            _, expiry = self.get('cache-control', 'max-age=60').split('=')
            await asyncio.sleep(int(expiry)/2)


class SSDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, server):
        self.log = logging.getLogger('{}.{}'.format(__name__, self.__class__.__name__))
        self.server = server

    def datagram_received(self, data, addr):
        lines = data.decode().splitlines()
        cmd = lines.pop(0)

        headers = {}
        for line in lines:
            try:
                key, value = line.split(':', 1)
                headers[key.lower()] = value.strip()
            except ValueError:
                pass

        if cmd.startswith('M-SEARCH *'):
            self.server.discoveryRequest(headers, addr)
        elif cmd.startswith('NOTIFY *'):
            self.log.debug('Notification %s from %s for %s', headers.get('nts'), addr, headers.get('nt'))
            if headers.get('nts') == 'ssdp:alive':
                self.server.register(headers)
            elif headers.get('nts') == 'ssdp:byebye':
                self.server.unregister(headers.get('usn'))
            else:
                self.log.warning('Unknown subtype %s for notification type %s', headers.get('nts'), headers.get('nt'))
        elif cmd.startswith('HTTP/1.1 200 OK'):
            headers['nt'] = headers.get('st')
            self.server.register(headers)
        else:
            self.log.warning('Unknown SSDP command %s\n%s', cmd, headers)


class SSDPMcastProtocol(SSDPProtocol):
    def connection_made(self, transport):
        sock = transport.get_extra_info('socket')
        sockname = transport.get_extra_info('sockname')
        group = socket.inet_aton(sockname[0])
        mreq = struct.pack('4sL', group, socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)


class SSDPServer:
    def __init__(self, loop=None):
        self.__log = logging.getLogger('{}.{}'.format(__name__, __class__.__name__))
        self.loop = loop or asyncio.get_event_loop()
        self.ssdpdevices = {}
        self.resend_notify_loop = None
        self.resend_mseatch_loop = None
        self.transport = None
        self.ucastprotocol = None

        mcast = self.loop.create_datagram_endpoint(
            lambda: SSDPMcastProtocol(self),
            local_addr=(SSDP_ADDR, SSDP_PORT), family=socket.AF_INET,
            reuse_port=True,
        )
        self.mtransport, self.mcastprotocol = self.loop.run_until_complete(mcast)


        self.resend_mseatch_loop = self.loop.create_task(self._resend_msearch())

    async def device_created(self, device:SSDPDevice):
        pass

    async def device_updated(self, device:SSDPDevice):
        pass

    async def device_removed(self, device:SSDPDevice):
        pass

    async def shutdown(self):
        for key in list(self.ssdpdevices):
            self.unregister(key)
        self.resend_mseatch_loop.cancel()
        try:
            await self.resend_mseatch_loop
        except asyncio.CancelledError:
            pass

    def register(self, headers, manifestation='remote', silent=False):
        self.__log.log(1, 'Register headers: %s', headers)
        if headers.get('usn') in self.ssdpdevices:
            self.__log.debug('updating last-seen for %r', headers.get('usn'))
            device = self.ssdpdevices.get(headers.get('usn'))
            current_host, current_port = urllib.parse.splitnport(urllib.parse.urlsplit(device.get('location')).netloc)
            headers_host, headers_port = urllib.parse.splitnport(urllib.parse.urlsplit(headers.get('location')).netloc)
            current_host = ipaddress.ip_address(current_host)
            headers_host = ipaddress.ip_address(headers_host)
            if current_port != headers_port or current_host.version == headers_host.version and current_host != headers_host:
                device.update(headers)
                if device.get('nt') == 'upnp:rootdevice':
                    self.loop.create_task(self.device_updated(device))
            device.ssdpalive()
        else:
            self.__log.info('Registering %s (%s)', headers.get('nt'), headers.get('location'))
            device = None
            if manifestation == 'remote':
                device = SSDPRemoteDevice(self, headers)
            elif manifestation == 'local':
                device = SSDPLocalDevice(self, headers)
                device['nts'] = 'ssdp:alive'
                device.silent = silent
            self.__log.info('device %s', device)
            if device:
                self.ssdpdevices[headers.get('usn')] = device

                if headers.get('nt') == 'upnp:rootdevice':
                    self.loop.create_task(self.device_created(device))

    def unregister(self, usn):
        if usn in self.ssdpdevices:
            self.__log.info("Un-registering %s", usn)
            if self.ssdpdevices[usn].get('nt') == 'upnp:rootdevice':
                self.loop.create_task(self.device_removed(self.ssdpdevices[usn]))
            self.ssdpdevices.pop(usn).shutdown()

    def discoveryRequest(self, headers, addr):
        (host, port) = addr
        self.__log.debug('Discovery request from %s:%d for %s', host, port, headers.get('st'))

    async def MSearch(self):
        req = ['M-SEARCH * HTTP/1.1',
               'HOST: %s:%d' % (SSDP_ADDR, SSDP_PORT),
               'MAN: "ssdp:discover"',
               'MX: 5',
               'ST: ssdp:all',
               'USER-AGENT: {}/{}'.format(__name__, version),
               '', '']
        req = '\r\n'.join(req)

        try:
            if self.transport is None or self.transport.is_closing():
                self.transport, self.ucastprotocol = await self.loop.create_datagram_endpoint(
                    lambda: SSDPProtocol(self),
                    family=socket.AF_INET, proto=socket.IPPROTO_UDP,
                    reuse_port=True,
                )

            self.__log.debug('send MSearch to %s:%d', SSDP_ADDR, SSDP_PORT)
            self.transport.sendto(req.encode(), (SSDP_ADDR, SSDP_PORT))
        except socket.error as msg:
            self.__log.info("failure sending out the discovery message: %r", msg)
        except Exception as msg:
            self.__log.exception("MSearch general failue")

    async def _resend_msearch(self):
        while True:
            await self.MSearch()
            await asyncio.sleep(120)
