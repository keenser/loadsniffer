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
    loop = asyncio.get_event_loop()

    httpport = 8444
    app = aiohttp.web.Application()
    upnpserver = upnp.UPNPServer(loop=loop, http=app, httpport=httpport)

    handler = app.make_handler()
    server = loop.create_server(handler, '0.0.0.0', httpport)
    server = loop.run_until_complete(server)
    print(server.sockets[0].getsockname())

    #upnpserver.ssdp.register({
    #    'usn':'uuid:8d43c269-a700-4541-81b9-1789c6149a1a::upnp:rootdevice',
    #    'nt': 'upnp:rootdevice',
    #    'location': 'http://192.168.1.145:5000/rootDesc.xml',
    #    'server': 'OpenWRT/OpenWrt UPnP/1.1 MiniUPnPd/2.0',
    #    'cache-control': 'max-age=60'
    #    },
    #    ('192.168.1.145', '5000'), manifestation='local'
    #)
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        upnpserver.shutdown()
        #server.close()
        #loop.run_until_complete(server.wait_closed())
        #loop.run_until_complete(app.shutdown())
        loop.run_until_complete(handler.shutdown(60.0))
        #loop.run_until_complete(app.cleanup())
        loop.close()


if __name__ == '__main__':
    main()
