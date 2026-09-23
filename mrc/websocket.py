#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
"""Websocket protocol for the web UI: a Hub (shared state) and one Connection per client."""

from __future__ import annotations
import logging
import json
import os.path
import urllib.parse
import mimetypes
from typing import Optional

import aiohttp
import aiohttp.web

import torrentstream

from .extract import Info


class Hub:
    """Shared state for every websocket connection: the upnp/torrent controllers, youtube-dl."""

    def __init__(self, loop, upnp, torrent: torrentstream.TorrentStream):
        self.log = logging.getLogger(self.__class__.__name__)
        self.loop = loop
        self.upnp = upnp
        self.torrent = torrent
        self.info = Info(loop)
        self.wsclients: set = set()

    async def websocket_handler(self, request: aiohttp.web.Request):
        self.log.debug('websocket_handler %s %s', request.remote, request.host)

        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        connection = Connection(
            hub=self,
            peer=request.remote,
            local=request.host,
            ws=ws
        )
        await connection.onOpen()
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    req = json.loads(msg.data)
                    data = req.pop('request', {})
                    await connection.onMessage(req, req.get('action'), data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except (OSError, TimeoutError):
            pass
        except Exception:
            self.log.exception('websocket_handler')
        finally:
            connection.onClose()

        return ws

    async def onShutdown(self, app):
        for connection in set(self.wsclients):
            await connection.ws.close(code=aiohttp.WSCloseCode.GOING_AWAY,
                                       message='Server shutdown')


class Connection:
    """One websocket client: message dispatch, plus the push handlers for a Hub's alerts."""

    def __init__(self, hub: Hub, peer, local, ws: aiohttp.web.WebSocketResponse):
        self.log = logging.getLogger(self.__class__.__name__)
        self.hub = hub
        self.peer = peer
        self.local = local
        self.ws = ws
        self._msg = None

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
                                self.hub.torrent.options['urlpath'],
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
        self.hub.wsclients.add(self)
        self.hub.upnp.add_alert_handler(self._upnpupdate)
        self.hub.torrent.add_alert_handler('files_list_update_alert', self._btupdate)
        self.hub.torrent.add_alert_handler('progress_update_alert', self._progressupdate)

    def onClose(self):
        self.log.info('WS client closed %s %s', self.peer, self.local)
        self.hub.wsclients.discard(self)
        self.hub.upnp.remove_alert_handler(self._upnpupdate)
        self.hub.torrent.remove_alert_handler('files_list_update_alert', self._btupdate)
        self.hub.torrent.remove_alert_handler('progress_update_alert', self._progressupdate)

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
                await self.hub.upnp.transporturi(url, data.get('title', 'Video'), data.get('relative', False))
        elif action == 'play':
            await self.hub.upnp.play()
        elif action == 'pause':
            await self.hub.upnp.pause()
        elif action == 'stop':
            await self.hub.upnp.stop()
        elif action == 'refresh':
            await self.hub.upnp.refresh()
        elif action == 'selectrenderer':
            self.hub.upnp.select_device(data.get('udn'))
        elif action == 'search':
            url = data.get('url')
            self.log.info('search %s', url)
            ret = await self.hub.info.youtube_dl(url)
            await self.sendMessage(ret)
        elif action == 'add':
            url = data.get('url')
            self.log.info('add %s', url)

            async def bittorrent():
                def add_torrent_alert(alert):
                    message = None
                    if alert.error.value() == 0:
                        message = 'done'
                    self.hub.torrent.remove_alert_handler('add_torrent', add_torrent_alert)
                    self.hub.loop.create_task(self.sendMessage(message, request))

                if self.hub.torrent.add_torrent(url):
                    self.hub.torrent.add_alert_handler('add_torrent', add_torrent_alert)
                else:
                    await self.sendMessage(None)

            ret = await self.hub.info.youtube_dl(url)
            if ret:
                await self.sendMessage(ret)
            else:
                await bittorrent()
        elif action == 'rm':
            url = data.get('url')
            self.hub.torrent.remove_torrent(url)
        elif action == 'load':
            info_hash = data.get('hash')
            self.hub.torrent.load_torrent(info_hash)
        elif action == 'btstatus':
            await self.sendMessage(self.btfileslist(self.hub.torrent.list_files()))
        elif action == 'upnpstatus':
            await self.sendMessage(self.hub.upnp.status)
        elif action == 'recheck':
            info_hash = data.get('url')
            self.hub.torrent.recheck(info_hash)
