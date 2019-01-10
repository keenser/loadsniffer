#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#

import asyncio
import logging
import aiohttp.web
from . import notify
from . import upnp


def main():
    logging.basicConfig(level=logging.WARN)

    httpport = 8444
    app = aiohttp.web.Application()
    upnpserver = upnp.UPNPServer(http=app, httpport=httpport)

    #upnpserver.ssdp.register({
    #    'usn':'uuid:8d43c269-a700-4541-81b9-1789c6149a1a::upnp:rootdevice',
    #    'nt': 'upnp:rootdevice',
    #    'location': 'http://192.168.1.145:5000/rootDesc.xml',
    #    'server': 'OpenWRT/OpenWrt UPnP/1.1 MiniUPnPd/2.0',
    #    'cache-control': 'max-age=60'
    #    },
    #    ('192.168.1.145', '5000'), manifestation='local'
    #)

    aiohttp.web.run_app(app, port=httpport, reuse_port=True)


if __name__ == '__main__':
    main()
