#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
# Media Renderer control server

from __future__ import annotations
import asyncio
import uvloop
import functools
import json
import urllib.parse
import os.path
import sys
import socket
import logging
import logging.handlers
import mimetypes
import multiprocessing
import traceback
import aiohttp
import aiohttp.web
import aioupnp
import torrentstream
import telnetlib3
from async_upnp_client.exceptions import UpnpError
from async_upnp_client.profiles.dlna import DmrDevice
from typing import Dict, Optional
from io import StringIO
import contextlib

have_youtube_dl = False
try:
    import youtube_dl
    # delete generic extractor
    youtube_dl.extractor.gen_extractor_classes().remove(youtube_dl.extractor.generic.GenericIE)
    have_youtube_dl = True
except ModuleNotFoundError:
    pass


class MediaDevice:
    def __init__(self, dmr: DmrDevice):
        self.dmr = dmr
        self.last_url: Optional[str] = None
        self.status = {'state': None, 'item': [], 'device': dmr.name}

    def __repr__(self):
        return "{} {}".format(self.dmr.name, self.status)


class UPnPctrl:
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
        self.registered_callbacks = {}

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
        self.device = mediadevice
        self.trigger_callbacks()

        dmr.on_event = functools.partial(self._on_dmr_event, mediadevice)

    def _on_dmr_event(self, mediadevice: MediaDevice, service, state_variables) -> None:
        dmr = mediadevice.dmr
        state = dmr.transport_state
        mediadevice.status['state'] = state.name if state else None
        mediadevice.status['item'] = (
            [{'url': mediadevice.last_url, 'title': dmr.media_title}] if dmr.media_title else []
        )
        self.log.info('%s now: %s %s', dmr.name, mediadevice.status['state'], dmr.media_title)
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
                await dmr.async_set_transport_uri(url, title)
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
        self.registered_callbacks[hash(callback)] = {'status': None, 'callback': callback}
        self.trigger_callbacks()

    def remove_alert_handler(self, callback):
        self.registered_callbacks.pop(hash(callback), None)

    def trigger_callbacks(self):
        for callback in self.registered_callbacks.values():
            try:
                status = self.device.status if self.device is not None else None
                if callback['status'] != status:
                    self.loop.create_task(callback['callback'](status))
                    if status:
                        callback['status'] = status.copy()
                    else:
                        callback['status'] = status
            except Exception as exeption:
                self.log.error('trigger_callbacks exception %s', exeption)

    async def refresh(self):
        await self.registry.async_search()


class CancellablePool:
    def __init__(self, max_workers=3):
        self._free = {self._new_pool() for _ in range(max_workers)}
        self._working = set()
        self._change = asyncio.Event()

    def _new_pool(self):
        return multiprocessing.Pool(1)

    async def apply(self, fn, *args):
        """
        Like multiprocessing.Pool.apply_async, but:
         * is an asyncio coroutine
         * terminates the process if cancelled
        """
        while not self._free:
            await self._change.wait()
            self._change.clear()
        pool = usable_pool = self._free.pop()
        self._working.add(pool)

        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        def _on_done(obj):
            loop.call_soon_threadsafe(fut.set_result, obj)
        def _on_err(err):
            loop.call_soon_threadsafe(fut.set_exception, err)
        pool.apply_async(fn, args, callback=_on_done, error_callback=_on_err)

        try:
            return await fut
        except asyncio.CancelledError:
            pool.terminate()
            usable_pool = self._new_pool()
        finally:
            self._working.remove(pool)
            self._free.add(usable_pool)
            self._change.set()

    def shutdown(self):
        for p in self._working | self._free:
            p.terminate()
        self._free.clear()


class Info:
    def __init__(self, loop):
        self.log = logging.getLogger(self.__class__.__name__)
        self.loop = loop

    @staticmethod
    def extract_info(url=None):
        try:
            ydl = youtube_dl.YoutubeDL(
                params={
                    'quiet': True,
                    'cachedir': '/tmp/',
                    'youtube_include_dash_manifest': False,
                    'prefer_ffmpeg': True,
                    'socket_timeout': 5,
                    'skip_download': True
                })
            stream = ydl.extract_info(url, False)
            data = {}
            data['src'] = stream.get('extractor')
            data['title'] = stream.get('title')
            data['url'] = stream.get('webpage_url')
            data['bitrate'] = []
            for i in stream.get('formats', []):
                if i.get('acodec') != 'none' and i.get('vcodec') != 'none':
                    data['bitrate'].append({'url':i.get('url'), 'bitrate':i.get('height') or i.get('format_id')})
            return data
        except Exception:
            pass

    async def youtube_dl(self, url):
        if not have_youtube_dl:
            return None
        pool = CancellablePool()
        task = self.loop.create_task(pool.apply(self.extract_info, url))
        try:
            return await asyncio.wait_for(task, 60)
        except Exception as exception:
            self.log.error('youtube_dl %s', exception)
        finally:
            pool.shutdown()


class WebSocketFactory:
    def __init__(self,
                 loop:Optional[asyncio.AbstractEventLoop]=None,
                 factory:Optional[WebSocketFactory]=None,
                 upnp:Optional[UPnPctrl]=None,
                 torrent:Optional[torrentstream.TorrentStream]=None,
                 peer:Optional[str]=None,
                 local:Optional[str]=None,
                 ws:Optional[aiohttp.web.WebSocketResponse]=None
        ):
        self.log = logging.getLogger(self.__class__.__name__)
        self._factory = factory
        self._upnp = upnp
        self._torrent = torrent
        self._msg = None
        self.loop = loop
        self.peer = peer
        self.local = local
        self.ws = ws
        self.wsclients = set()
        self.info = Info(self.factory.loop) if factory is None else None
        super().__init__()

    @property
    def factory(self) -> WebSocketFactory:
        return self._factory or self

    @property
    def upnp(self):
        assert self.factory._upnp
        return self.factory._upnp

    @property
    def torrent(self):
        assert self.factory._torrent
        return self.factory._torrent

    async def websocket_handler(self, request:aiohttp.web.Request):
        self.log.debug('websocket_handler %s %s', request.remote, request.host)

        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        wsclient = WebSocketFactory(
            factory=self.factory,
            peer=request.remote,
            local=request.host,
            ws=ws
        )
        await wsclient.onOpen()
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    req = json.loads(msg.data)
                    data = req.pop('request', {})
                    await wsclient.onMessage(req, req.get('action'), data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except (OSError, TimeoutError):
            pass
        except Exception as e:
            self.log.exception('websocket_handler')
        finally:
            wsclient.onClose()

        return ws

    @staticmethod
    def videofiles(files):
        return files
        ret = []
        for i in files:
            mime = mimetypes.guess_type(i['path'], strict=False)[0]
            if mime and mime.startswith('video'):
                ret.append(i)
        return ret

    def btfileslist(self, infiles):
        for handle in infiles:
            for i in self.videofiles(handle['files']):
                i['title'] = os.path.basename(i['path'])
                i['url'] = urllib.parse.urljoin(
                                self.torrent.options['urlpath'],
                                urllib.parse.quote(i['path']))
        return infiles

    async def _btupdate(self, alert):
        await self.sendMessage(self.btfileslist(alert.files), {'action': 'btstatus'})

    async def _upnpupdate(self, message):
        await self.sendMessage(message, {'action': 'upnpstatus'})

    async def _progressupdate(self, alert):
        await self.sendMessage(alert.progress, {'action': 'progressupdate'})

    async def onOpen(self):
        self.log.info('WS client connected %s %s', self.peer, self.local)
        self.factory.wsclients.add(self)
        self.upnp.add_alert_handler(self._upnpupdate)
        self.torrent.add_alert_handler('files_list_update_alert', self._btupdate)
        self.torrent.add_alert_handler('progress_update_alert', self._progressupdate)

    def onClose(self):
        self.log.info('WS client closed %s %s', self.peer, self.local)
        self.factory.wsclients.discard(self)
        self.upnp.remove_alert_handler(self._upnpupdate)
        self.torrent.remove_alert_handler('files_list_update_alert', self._btupdate)
        self.torrent.remove_alert_handler('progress_update_alert', self._progressupdate)

    async def onShutdown(self, app):
        for wsclient in set(self.wsclients):
            await wsclient.ws.close(code=aiohttp.WSCloseCode.GOING_AWAY,
                                    message='Server shutdown')

    async def sendMessage(self, message, request: Optional[dict] = None) -> None:
        if request is None:
            request = self._msg
        if request:
            request['response'] = message
            await self.ws.send_json(request)

    async def onMessage(self, request: dict, action: str, data: dict) -> None:
        self.log.debug('onMessage action: %s', action)
        self._msg = request
        if action == 'transporturi':
            url = data.get('url')
            if url:
                if data.get('cookie'):
                    #TODO
                    print("cookie", data.get('cookie'))
                    url = "http://{}:8080/?url={}&cookie={}".format(self.local, urllib.parse.quote(url), urllib.parse.quote(data.get('cookie')))
                self.log.info('push to play relative %s, url: %s', data.get('relative'), url)
                await self.upnp.transporturi(url, data.get('title', 'Video'), data.get('relative', False))
        elif action == 'play':
            await self.upnp.play()
        elif action == 'pause':
            await self.upnp.pause()
        elif action == 'stop':
            await self.upnp.stop()
        elif action == 'refresh':
            await self.upnp.refresh()
        elif action == 'search':
            url = data.get('url')
            self.log.info('search %s', url)
            ret = await self.factory.info.youtube_dl(url)
            await self.sendMessage(ret)
        elif action == 'add':
            url = data.get('url')
            self.log.info('add %s', url)

            async def bittorrent():
                def add_torrent_alert(alert):
                    message = None
                    if alert.error.value() == 0:
                        message = 'done'
                    self.torrent.remove_alert_handler('add_torrent', add_torrent_alert)
                    self.factory.loop.create_task(self.sendMessage(message, request))

                if self.torrent.add_torrent(url):
                    self.torrent.add_alert_handler('add_torrent', add_torrent_alert)
                else:
                    await self.sendMessage(None)

            ret = await self.factory.info.youtube_dl(url)
            if ret:
                await self.sendMessage(ret)
            else:
                await bittorrent()
        elif action == 'rm':
            url = data.get('url')
            self.torrent.remove_torrent(url)
        elif action == 'load':
            info_hash = data.get('hash')
            self.torrent.load_torrent(info_hash)
        elif action == 'btstatus':
            await self.sendMessage(self.btfileslist(self.torrent.list_files()))
        elif action == 'upnpstatus':
            message = self.upnp.device.status if self.upnp.device else None
            await self.sendMessage(message)
        elif action == 'recheck':
            info_hash = data.get('url')
            self.torrent.recheck(info_hash)


async def rootindex(app, handler):
    async def index_handler(request):
        if request.path == '/':
            request.match_info['filename'] = 'index.html'
        return await handler(request)
    return index_handler

@contextlib.contextmanager
def stdoutIO(stdout=None):
    old = sys.stdout
    if stdout is None:
        stdout = StringIO()
    sys.stdout = stdout
    yield stdout
    sys.stdout = old

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
    logging.getLogger('WebSocketFactory').setLevel(logging.INFO)
    logging.getLogger('torrent').setLevel(logging.DEBUG)
    logging.getLogger('aioupnp').setLevel(logging.INFO)
    logging.getLogger('aiohttp.access').setLevel(logging.WARN)

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
    ws = WebSocketFactory(loop=loop, upnp=upnp, torrent=torrent)
    http.on_shutdown.append(ws.onShutdown)

    mediaserver = aioupnp.MediaServer(
        list_containers=content_containers,
        list_items=content_items,
        friendly_name='loadsniffer',
        source=(host_ip, 0),
    )
    torrent.add_alert_handler('files_list_update_alert', lambda alert: mediaserver.on_files_changed())

    http.add_subapp(torrent.options['urlpath'], torrent.http)
    http.router.add_get('/ws', ws.websocket_handler)
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

    class console(asyncio.Protocol):
        def __init__(self):
            super().__init__()
            self.transport:asyncio.Transport
            self.torrent = torrent
            self.upnp = upnp
            self.ws = ws

        def connection_made(self, transport:asyncio.Transport):
            self.transport = transport

        def data_received(self, data):
            with stdoutIO() as s:
                try:
                    exec(data)
                except Exception as e:
                    self.transport.write('{}\n'.format(e).encode())
                self.transport.write('{}\n'.format(s.getvalue()).encode())


    async def shell(reader, writer):
        """
        A default telnet shell, appropriate for use with telnetlib3.create_server.

        This shell provides a very simple REPL, allowing introspection and state
        toggling of the connected client session.
        """
        nonlocal upnp, torrent, ws
        CR = telnetlib3.server_shell.CR
        LF = telnetlib3.server_shell.LF
        writer.write("Ready." + CR + LF)

        linereader = telnetlib3.server_shell.readline(reader, writer)
        linereader.send(None)

        command = None
        while True:
            if command:
                writer.write(CR + LF)
            writer.write("tel:sh> ")
            command = None
            while command is None:
                await writer.drain()
                inp = await reader.read(1)
                if not inp:
                    return
                command = linereader.send(inp)
            writer.write(CR + LF)
            if command == "quit":
                writer.write("Goodbye." + CR + LF)
                break
            else:
                with stdoutIO() as s:
                    try:
                        exec(command)
                    except Exception as e:
                        writer.write('{}\n'.format(e))
                    writer.write('{}\n'.format(s.getvalue()))

        writer.close()

    #cons = loop.run_until_complete(loop.create_server(console, '127.0.0.1', 8888))
    cons = loop.run_until_complete(telnetlib3.create_server(port=8888, shell=shell))

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
