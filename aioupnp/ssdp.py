#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#

import asyncio
import logging
import socket
import struct
from . import notify
from . import version

SSDP_PORT = 1900
SSDP_ADDR = '239.255.255.250'

class SSDPDevice(dict):
    def __init__(self, server, data={}):
        super().__init__(data)
        self.log = logging.getLogger(self.__class__.__name__)
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
            tuple('{}: {}'.format(x.upper(), self.get(x)) for x in ['nts', 'usn', 'nt', 'location', 'server', 'cache-control'])
            +
            ('','')
        ).encode()

    def ssdpalive(self, data={}):
        pass

    def doNotify(self):
        self.log.info('Sending %s notification for %s', self.get('nts'), self.get('usn'))
        self.log.debug('doNotify content %s', self)
        try:
            self.server.transport.sendto(bytes(self), (SSDP_ADDR, SSDP_PORT))
        except (AttributeError, socket.error) as msg:
            self.log.info("failure sending out alive notification: %r", msg)


class SSDPRemoteDevice(SSDPDevice):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.manifestation = 'remote'
        self.ssdpalive()

    def ssdpalive(self, data={}):
        if self.handle:
            self.handle.cancel()

        if 'location' in data and data.get('location') != self.get('location'):
            self.update(data)
            if self.get('nt') == 'upnp:rootdevice':
                notify.send('UPnP.SSDP.update_device', device=self)

        _, expiry = self.get('cache-control', 'max-age=60').split('=')
        self.handle = self.server.loop.call_later(int(expiry) + 5, self.server.unRegister, self.get('usn'))


class SSDPLocalDevice(SSDPDevice):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.manifestation = 'local'
        self['nts'] = 'ssdp:alive'
        if not self.silent:
            self.handle = self.server.loop.create_task(self.resendNotify())

    def shutdown(self):
        self['nts'] = 'ssdp:byebye'
        if not self.silent:
            self.doNotify()
        super().shutdown()

    async def resendNotify(self):
        while True:
            self.doNotify()
            _, expiry = self.get('cache-control', 'max-age=60').split('=')
            await asyncio.sleep(int(expiry)/2)


class SSDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, server):
        self.log = logging.getLogger(self.__class__.__name__)
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
            self.server.notifyReceived(headers, addr)
        elif cmd.startswith('HTTP/1.1 200 OK'):
            headers['nt'] = headers.get('st')
            self.server.register(headers, addr)
        else:
            self.log.warning('Unknown SSDP command %s', cmd)
            self.log.warning(headers)


class SSDPMcastProtocol(SSDPProtocol):
    def connection_made(self, transport):
        sock = transport.get_extra_info('socket')
        sockname = transport.get_extra_info('sockname')
        group = socket.inet_aton(sockname[0])
        mreq = struct.pack('4sL', group, socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)


class SSDPServer:
    def __init__(self, loop=None):
        self.log = logging.getLogger(self.__class__.__name__)
        self.loop = asyncio.get_event_loop() if loop is None else loop
        self.devices = {}
        self.resend_notify_loop = None
        self.resend_mseatch_loop = None

    async def __await__(self):
        mcast = self.loop.create_datagram_endpoint(
            lambda: SSDPMcastProtocol(self),
            local_addr=(SSDP_ADDR, SSDP_PORT), family=socket.AF_INET
        )
        _, self.mcastprotocol = await self.loop.create_task(mcast)
        ucast = self.loop.create_datagram_endpoint(
            lambda: SSDPProtocol(self),
            family=socket.AF_INET, proto=socket.IPPROTO_UDP
        )
        self.transport, self.ucastprotocol = await self.loop.create_task(ucast)
        self.resend_mseatch_loop = self.loop.create_task(self.resendMSearch())
        return self

    def shutdown(self):
        for key in list(self.devices):
            self.unRegister(key)
        self.resend_mseatch_loop.cancel()
        try:
            self.loop.run_until_complete(self.resend_mseatch_loop)
        except asyncio.CancelledError:
            pass

    def register(self, headers, addr, manifestation='remote', silent=False):
        #(host, port) = addr
        if headers.get('usn') in self.devices:
            self.log.debug('updating last-seen for %r', headers.get('usn'))
            self.devices.get(headers.get('usn')).ssdpalive(headers)
        else:
            self.log.info('Registering %s (%s)', headers.get('nt'), headers.get('location'))
            if manifestation == 'remote':
                device = SSDPRemoteDevice(self, headers) #{k: headers.get(k) for k in ['usn', 'nt', 'location', 'server', 'cache-control']})
            elif manifestation == 'local':
                device = SSDPLocalDevice(self, headers)
                device['nts'] = 'ssdp:alive'
                device.silent = silent
            self.devices[headers.get('usn')] = device

            if headers.get('nt') == 'upnp:rootdevice':
                notify.send('UPnP.SSDP.new_device', device=device)

    def unRegister(self, usn):
        if usn in self.devices:
            self.log.info("Un-registering %s", usn)
            if self.devices[usn].get('nt') == 'upnp:rootdevice':
                notify.send('UPnP.SSDP.removed_device', device=self.devices[usn])
            self.devices.pop(usn).shutdown()

    def notifyReceived(self, headers, addr):
        (host, port) = addr
        self.log.debug('Notification %s from %s:%d for %s', headers.get('nts'), host, port, headers.get('nt'))
        self.log.log(1, 'Notification headers: %s', headers)

        if headers.get('nts') == 'ssdp:alive':
            self.register(headers, addr)
        elif headers.get('nts') == 'ssdp:byebye':
            self.unRegister(headers.get('usn'))
        else:
            self.log.warning('Unknown subtype %s for notification type %s', headers.get('nts'), headers.get('nt'))

    def discoveryRequest(self, headers, addr):
        (host, port) = addr
        self.log.debug('Discovery request from %s:%d for %s', host, port, headers.get('st'))
        #self.log.info('Discovery request for %s', headers.get('st'))

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
        except (socket.error) as msg:
            self.log.info("failure sending out the discovery message: %r", msg)

    async def resendMSearch(self):
        while True:
            self.MSearch()
            await asyncio.sleep(120)

