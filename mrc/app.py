#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
# Media Renderer control server

import asyncio
import uvloop
import mimetypes
import os
import os.path
import sys
import socket
import logging
import traceback
import urllib.parse
import aiohttp
import aiohttp.web
import aioupnp
import torrentstream

from .devices import UPnPctrl
from .websocket import Hub
from . import telnet


async def rootindex(app, handler):
    async def index_handler(request):
        if request.path == '/':
            request.match_info['filename'] = 'index.html'
        return await handler(request)
    return index_handler


def main():
    if 'JOURNAL_STREAM' in os.environ:
        logformat = '%(levelname)s:%(name)s: %(message)s'
    else:
        logformat = '%(asctime)s %(levelname)s:%(name)s: %(message)s'
    logging.basicConfig(level=logging.INFO, format=logformat)
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def exception_handler(loop, context):
        logging.error('exception_handler: %s', context)
        if 'exception' in context:
            logging.error('traceback %s', ''.join(traceback.format_list(traceback.extract_tb(context['exception'].__traceback__))))

    loop.set_exception_handler(exception_handler)

    logging.getLogger('UPnPctrl').setLevel(logging.INFO)
    logging.getLogger('Hub').setLevel(logging.INFO)
    logging.getLogger('Connection').setLevel(logging.INFO)
    logging.getLogger('torrent').setLevel(logging.WARN)
    logging.getLogger('aioupnp').setLevel(logging.INFO)
    logging.getLogger('aiohttp.access').setLevel(logging.WARN)
    logging.getLogger('async_upnp_client.traffic.upnp').setLevel(logging.DEBUG)

    httpport = 8883
    # TODO: use argparse
    save_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/'

    def lan_ip() -> str:
        """best-guess LAN-facing IP, used both to bind SSDP sockets to a concrete interface
        (rather than 0.0.0.0, which is ambiguous for IP_ADD_MEMBERSHIP on multi-homed hosts,
        e.g. any host that also runs Docker's own bridge interfaces) and to build absolute
        URLs advertised to DLNA clients browsing the MediaServer"""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.connect(('8.8.8.8', 80))
                return probe.getsockname()[0]
            except OSError:
                return socket.gethostbyname(socket.gethostname())

    host_ip = lan_ip()

    def content_containers():
        return [{'id': data['info_hash'], 'title': data['title']} for data in torrent.list_files()]

    def content_items(container_id):
        base = 'http://{}:{}{}'.format(host_ip, httpport, torrent.options['urlpath'])
        for data in torrent.list_files():
            if data['info_hash'] != container_id:
                continue
            return [{
                'id': file['id'],
                'title': os.path.basename(file['path']),
                'url': urllib.parse.urljoin(base, urllib.parse.quote(file['path'])),
                'mime': mimetypes.guess_type(file['path'], strict=False)[0],
            } for file in data['files']]
        return []

    http = aiohttp.web.Application(middlewares=[rootindex])
    upnp = UPnPctrl(loop=loop, http=http, httpport=httpport, source=(host_ip, 0))
    torrent = torrentstream.TorrentStream(loop=loop, save_path=save_path, urlpath='/bt/')
    hub = Hub(loop=loop, upnp=upnp, torrent=torrent)
    http.on_shutdown.append(hub.onShutdown)

    mediaserver = aioupnp.MediaServer(
        list_containers=content_containers,
        list_items=content_items,
        friendly_name='loadsniffer',
        source=(host_ip, 0),
    )
    torrent.add_alert_handler('files_list_update_alert', lambda alert: mediaserver.on_files_changed())

    http.add_subapp(torrent.options['urlpath'], torrent.http)
    http.router.add_get('/ws', hub.websocket_handler)
    http.router.add_static('/', 'static')

    loop.run_until_complete(upnp.start())
    loop.run_until_complete(mediaserver.start())

    logging.info('listening aiohttp server %s on port %d', aiohttp.__version__, httpport)

    runner = aiohttp.web.AppRunner(http)
    loop.run_until_complete(runner.setup())
    site = aiohttp.web.TCPSite(runner, None, httpport, reuse_port=True)
    loop.run_until_complete(site.start())

    for sock in site._server.sockets:
        sock.setsockopt(socket.SOL_IP, socket.IP_TOS, 160)

    cons = loop.run_until_complete(telnet.start(8888, {'torrent': torrent, 'upnp': upnp, 'hub': hub}))

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(mediaserver.stop())
        loop.run_until_complete(upnp.shutdown())
        loop.run_until_complete(runner.cleanup())
        cons.close()
        tasks = asyncio.all_tasks(loop)
        expensive_tasks = {task for task in tasks if not task.done()}
        loop.run_until_complete(asyncio.gather(*expensive_tasks))
        loop.close()


if __name__ == '__main__':
    main()
