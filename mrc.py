#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
# Media Renderer control server

import asyncio
import json
import urllib.parse
import os.path
import sys
import logging
import logging.handlers
import mimetypes
import multiprocessing
import aiohttp
import aioupnp
import torrentstream

#delete generic extractor
have_youtube_dl = False
try:
    import youtube_dl
    youtube_dl.extractor._ALL_CLASSES.pop()
    have_youtube_dl = True
except ModuleNotFoundError:
    pass

class MediaDevice:
    def __init__(self, device):
        self.media = device
        self.status = {'state': None, 'item': [], 'device': device.friendlyName}

    def __repr__(self):
        return "{} {}".format(self.media, self.status)

class UPnPctrl:
    def __init__(self, loop=None, http=None, httpport=0):
        self.log = logging.getLogger(self.__class__.__name__)
        self.aioupnp = aioupnp.upnp.UPNPServer(loop=loop, http=http, httpport=httpport)
        aioupnp.notify.connect('UPnP.Device.detection_completed', self.media_renderer_found)
        aioupnp.notify.connect('UPnP.RootDevice.removed', self.media_renderer_removed)

        self.loop = loop or asyncio.get_event_loop()
        self.mediadevices = {}
        self.device = None
        self.registered_callbacks = {}

    def shutdown(self):
        self.aioupnp.shutdown()

    async def media_renderer_removed(self, device=None):
        self.log.info('media_renderer_removed %s', device)
        self.mediadevices.pop(device.usn, None)
        if self.device:
            if self.device.media.usn == device.usn:
                if self.mediadevices:
                    self.device = list(self.mediadevices.values())[0]
                else:
                    self.device = None
                self.trigger_callbacks()

    async def media_renderer_found(self, device=None):
        if device is None:
            return

        self.log.info('found upnp device %s %s', device.usn, device.friendlyName)

        if device.deviceType.find('MediaRenderer') < 0:
            return

        self.log.info('media renderer %s', device.friendlyName)

        mediadevice = MediaDevice(device)
        self.mediadevices[device.usn] = mediadevice
        #if not self.device:
        self.device = mediadevice
        self.trigger_callbacks()

        service = device.service('AVTransport')
        await service.subscribe('CurrentTrackMetaData', self.state_variable_change)
        await service.subscribe('TransportState', self.state_variable_change)

    async def play(self, url, title='Video', vtype='video/mp4'):
        if self.device:
            try:
                async with aiohttp.ClientSession(read_timeout=5) as session:
                    async with session.head(url) as response:
                        ctype = response.headers.get('content-type', vtype)
                        service = self.device.media.service('AVTransport')
                        try:
                            await service.stop()
                        except aiohttp.client_exceptions.ClientError:
                            pass
                        await service.transporturi(url, title, ctype)
                        await service.play()
            except (OSError, asyncio.TimeoutError, aiohttp.client_exceptions.ClientError) as err:
                self.log.warning('play %s', err)
                return

    def add_alert_handler(self, callback):
        self.registered_callbacks[id(callback)] = {'status': None, 'callback': callback}
        self.trigger_callbacks()

    def remove_alert_handler(self, callback):
        self.registered_callbacks.pop(id(callback), None)

    def state_variable_change(self, variable):
        usn = variable.service.device.usn
        if variable.name == 'CurrentTrackMetaData':
            self.log.info('%s changed from %s to %s', variable.name, variable.old_value, variable.value)
            if variable.value is not None and variable.value:
                try:
                    elt = aioupnp.dlna.didl.fromString(variable.value)
                    self.mediadevices[usn].status['item'] = []
                    self.log.info('now playing: %s %s', elt['DIDL-Lite']['item']['dc:title'], elt['DIDL-Lite']['item']['@id'])
                    self.mediadevices[usn].status['item'].append({
                        'url':   elt['DIDL-Lite']['item']['@id'],
                        'title': elt['DIDL-Lite']['item']['dc:title']
                    })
                    #for item in elt.getItems():
                    #    print("now playing:", item.title, item.id)
                    #    self.mediadevices[usn].status['item'].append({'url':item.id, 'title':item.title})
                except SyntaxError:
                    return
        elif variable.name == 'TransportState':
            self.log.info('%s changed from %s to %s', variable.name, variable.old_value, variable.value)
            self.mediadevices[usn].status['state'] = variable.value
        self.trigger_callbacks()

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
        await self.aioupnp.ssdp.resendMSearch()


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
    def __init__(self, loop=None, factory=None, upnp=None, torrent=None, peer=None, local=None, ws=None):
        self.log = logging.getLogger(self.__class__.__name__)
        self._factory = factory
        self._upnp = upnp
        self._torrent = torrent
        self._upnpupdate = None
        self._btupdate = None
        self.loop = loop
        self.peer = peer
        self.local = local
        self.ws = ws
        self.wsclients = set()
        self.info = Info(self.factory.loop) if factory is None else None
        super().__init__()

    @property
    def factory(self):
        return self._factory or self

    @property
    def upnp(self):
        return self.factory._upnp

    @property
    def torrent(self):
        return self.factory._torrent

    async def websocket_handler(self, request):
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
                    await wsclient.onMessage(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except (OSError, TimeoutError):
            pass
        except Exception as e:
            self.log.error('websocket_handler %s', e)
        finally:
            wsclient.onClose()

        return ws

    @staticmethod
    def videofiles(files):
        ret = []
        for i in files:
            mime = mimetypes.guess_type(i, strict=False)[0]
            if mime and mime.startswith('video'):
                ret.append(i)
        return ret

    def btfileslist(self, infiles):
        response = []
        for handle in infiles:
            data = {}
            data['info_hash'] = handle['info_hash']
            data['title'] = handle['title']
            data['files'] = [{
                'title': os.path.basename(i),
                'url':
                    urllib.parse.urljoin(
                        urllib.parse.urlunsplit(['http', self.local, self.torrent.options.get('urlpath'), None, None]),
                        urllib.parse.quote(i)
                    )
                } for i in self.videofiles(handle['files'])]
            response.append(data)
        return response

    async def onOpen(self):
        async def upnpupdate(message):
            await self.sendMessage(message, {'action':'upnpstatus'})

        async def btupdate(alert):
            await self.sendMessage(self.btfileslist(alert.files), {'action':'btstatus'})

        self.log.info('WS client connected %s %s', self.peer, self.local)
        self.factory.wsclients.add(self)
        # handle function id must be same on adding and removing alert
        self._upnpupdate = upnpupdate
        self._btupdate = btupdate
        self.upnp.add_alert_handler(self._upnpupdate)
        self.torrent.add_alert_handler('files_list_update_alert', self._btupdate)

    def onClose(self):
        self.log.info('WS client closed %s %s with exception: %s', self.peer, self.local, self.ws.exception())
        self.factory.wsclients.discard(self)
        if self._upnpupdate:
            self.upnp.remove_alert_handler(self._upnpupdate)
        if self._btupdate:
            self.torrent.remove_alert_handler('files_list_update_alert', self._btupdate)

    async def onShutdown(self, app):
        for wsclient in set(self.wsclients):
            await wsclient.ws.close(code=aiohttp.WSCloseCode.GOING_AWAY,
                                    message='Server shutdown')

    async def sendMessage(self, message, request):
        request['response'] = message
        await self.ws.send_json(request)

    async def onMessage(self, payload):
        jsondata = json.loads(payload)
        if jsondata.get('action') == 'play':
            data = jsondata.pop('request', {})
            url = data.get('url')
            if url:
                if data.get('cookie'):
                    #TODO
                    print("cookie", data.get('cookie'))
                    url = "http://{}:8080/?url={}&cookie={}".format(self.local, urllib.parse.quote(url), urllib.parse.quote(data.get('cookie')))
                self.log.info('push to play url: %s', url)
                #TODO: change url if localhost
                await self.upnp.play(url, data.get('title', 'Video'))
        elif jsondata.get('action') == 'refresh':
            await self.upnp.refresh()
        elif jsondata.get('action') == 'search':
            data = jsondata.pop('request', {})
            url = data.get('url')
            self.log.info('search %s', url)
            ret = await self.factory.info.youtube_dl(url)
            await self.sendMessage(ret, jsondata)
        elif jsondata.get('action') == 'add':
            data = jsondata.pop('request', {})
            url = data.get('url')
            self.log.info('add %s', url)
            async def bittorrent():
                def remove_handlers():
                    self.torrent.remove_alert_handler('torrent_error_alert', torrent_error_alert)
                    self.torrent.remove_alert_handler('tracker_announce_alert', tracker_announce_alert)
                async def torrent_error_alert(alert):
                    self.log.info('torrent_error_alert %s', jsondata)
                    await self.sendMessage(None, jsondata)
                    remove_handlers()
                async def tracker_announce_alert(alert):
                    await self.sendMessage('done', jsondata)
                    remove_handlers()
                if self.torrent.add_torrent(url):
                    self.torrent.add_alert_handler('torrent_error_alert', torrent_error_alert)
                    self.torrent.add_alert_handler('tracker_announce_alert', tracker_announce_alert)
                else:
                    await self.sendMessage(None, jsondata)

            ret = await self.factory.info.youtube_dl(url)
            if ret:
                await self.sendMessage(ret, jsondata)
            else:
                await bittorrent()
        elif jsondata.get('action') == 'rm':
            data = jsondata.pop('request', {})
            url = data.get('url')
            self.torrent.remove_torrent(url)
        elif jsondata.get('action') == 'btstatus':
            await self.sendMessage(self.btfileslist(self.torrent.list_files()), jsondata)
        elif jsondata.get('action') == 'upnpstatus':
            message = self.upnp.device.status if self.upnp.device else None
            await self.sendMessage(message, jsondata)

async def rootindex(app, handler):
    async def index_handler(request):
        if request.path == '/':
            request.match_info['filename'] = 'index.html'
        return await handler(request)
    return index_handler

def main():
    logging.basicConfig(level=logging.DEBUG)
    loop = asyncio.get_event_loop()

    def exception_handler(loop, context):
        logging.warning('loop: %s', context)

    loop.set_exception_handler(exception_handler)

    logging.getLogger('SSDPServer').setLevel(logging.WARN)
    logging.getLogger('EventsServer').setLevel(logging.INFO)
    logging.getLogger('TorrentProducer').setLevel(logging.INFO)
    logging.getLogger('TorrentStream').setLevel(logging.INFO)
    logging.getLogger('WebSocketFactory').setLevel(logging.INFO)

    httpport = 8883
    # TODO: use argparse
    save_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/'

    http = aiohttp.web.Application(middlewares=[rootindex])
    upnp = UPnPctrl(loop=loop, http=http, httpport=httpport)
    torrent = torrentstream.TorrentStream(loop=loop, save_path=save_path, urlpath='/bt/')
    ws = WebSocketFactory(loop=loop, upnp=upnp, torrent=torrent)
    http.on_shutdown.append(ws.onShutdown)

    http.add_subapp(torrent.options['urlpath'], torrent.http)
    http.router.add_get('/ws', ws.websocket_handler)
    http.router.add_static('/', 'static')

    logging.info('listening aiohttp server %s on port %d', aiohttp.__version__, httpport)
    handler = http.make_handler()
    server = loop.create_server(handler, None, httpport)
    server = loop.run_until_complete(server)

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        upnp.shutdown()
        loop.run_until_complete(http.shutdown())
        loop.run_until_complete(handler.shutdown(60.0))
        loop.close()

    #site.socket.setsockopt(socket.SOL_IP, socket.IP_TOS, 160)

if __name__ == '__main__':
    main()
