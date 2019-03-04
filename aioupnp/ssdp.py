#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#

import asyncio
import logging
import socket
import struct
import urllib.parse
from . import notify
from . import version

SSDP_PORT = 1900
SSDP_ADDR = '239.255.255.250'


class SSDPDevice(dict):
    render_headers = ['nts', 'usn', 'nt', 'location', 'server', 'cache-control']

    def __init__(self, server, data={}):
        super().__init__(data)
        self.log = logging.getLogger('{}.{}'.format(__name__, self.__class__.__name__))
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
        self.log = logging.getLogger('{}.{}'.format(__name__, self.__class__.__name__))
        self.loop = loop or asyncio.get_event_loop()
        self.devices = {}
        self.resend_notify_loop = None
        self.resend_mseatch_loop = None

        mcast = self.loop.create_datagram_endpoint(
            lambda: SSDPMcastProtocol(self),
            local_addr=(SSDP_ADDR, SSDP_PORT), family=socket.AF_INET
        )
        _, self.mcastprotocol = self.loop.run_until_complete(mcast)

        ucast = self.loop.create_datagram_endpoint(
            lambda: SSDPProtocol(self),
            family=socket.AF_INET, proto=socket.IPPROTO_UDP
        )
        self.transport, self.ucastprotocol = self.loop.run_until_complete(ucast)

        self.resend_mseatch_loop = self.loop.create_task(self._resend_msearch())

    async def shutdown(self):
        for key in list(self.devices):
            self.unregister(key)
        self.resend_mseatch_loop.cancel()
        try:
            await self.resend_mseatch_loop
        except asyncio.CancelledError:
            pass

    def register(self, headers, manifestation='remote', silent=False):
        self.log.log(1, 'Register headers: %s', headers)
        if headers.get('usn') in self.devices:
            self.log.debug('updating last-seen for %r', headers.get('usn'))
            device = self.devices.get(headers.get('usn'))
            _, current_port = urllib.parse.splitnport(urllib.parse.urlsplit(device.get('location')).netloc)
            _, headers_port = urllib.parse.splitnport(urllib.parse.urlsplit(headers.get('location')).netloc)
            if current_port != headers_port:
                device.update(headers)
                if device.get('nt') == 'upnp:rootdevice':
                    notify.send('UPnP.SSDP.update_device', device=device)
            device.ssdpalive()
        else:
            self.log.info('Registering %s (%s)', headers.get('nt'), headers.get('location'))
            device = None
            if manifestation == 'remote':
                device = SSDPRemoteDevice(self, headers)
            elif manifestation == 'local':
                device = SSDPLocalDevice(self, headers)
                device['nts'] = 'ssdp:alive'
                device.silent = silent
            if device:
                self.devices[headers.get('usn')] = device

                if headers.get('nt') == 'upnp:rootdevice':
                    notify.send('UPnP.SSDP.new_device', device=device)

    def unregister(self, usn):
        if usn in self.devices:
            self.log.info("Un-registering %s", usn)
            if self.devices[usn].get('nt') == 'upnp:rootdevice':
                notify.send('UPnP.SSDP.removed_device', device=self.devices[usn])
            self.devices.pop(usn).shutdown()

    def discoveryRequest(self, headers, addr):
        (host, port) = addr
        self.log.debug('Discovery request from %s:%d for %s', host, port, headers.get('st'))

    def MSearch(self):
        req = ['M-SEARCH * HTTP/1.1',
               'HOST: %s:%d' % (SSDP_ADDR, SSDP_PORT),
               'MAN: "ssdp:discover"',
               'MX: 5',
               'ST: ssdp:all',
               'USER-AGENT: {}/{}'.format(__name__, version),
               '', '']
        req = '\r\n'.join(req)

        try:
            self.transport.sendto(req.encode(), (SSDP_ADDR, SSDP_PORT))
        except socket.error as msg:
            self.log.info("failure sending out the discovery message: %r", msg)

    async def _resend_msearch(self):
        while True:
            self.MSearch()
            await asyncio.sleep(120)
