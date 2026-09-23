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
from typing import List, Optional

import aiohttp
import aiohttp.web
from pydantic import BaseModel, ValidationError

import torrentstream

from .extract import Info


class TransportUriRequest(BaseModel):
    url: str
    title: str = 'Video'
    cookie: Optional[str] = None
    relative: bool = False


class SelectRendererRequest(BaseModel):
    udn: Optional[str] = None


class UrlRequest(BaseModel):
    """Shared shape for search/add/rm/recheck - all send {"url": "..."}.

    For rm/recheck this is actually an info_hash (existing wire format from
    static/mrc.js and the chrome/ extension - kept as-is, not renamed).
    """
    url: str


class LoadRequest(BaseModel):
    hash: str


class BtFileEntry(BaseModel):
    path: str
    id: int
    progress: float
    title: str
    url: str


class BtTorrentEntry(BaseModel):
    info_hash: str
    title: str
    progress: float
    files: List[BtFileEntry] = []


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
            mime = mimetypes.guess_type(i.path, strict=False)[0]
            if mime and mime.startswith('video'):
                ret.append(i)
        return ret

    def btfileslist(self, infiles: List[torrentstream.TorrentEntry]) -> List[BtTorrentEntry]:
        result = []
        for handle in infiles:
            files = [
                BtFileEntry(
                    path=f.path,
                    id=f.id,
                    progress=f.progress,
                    title=os.path.basename(f.path),
                    url=urllib.parse.urljoin(
                        self.hub.torrent.options['urlpath'],
                        urllib.parse.quote(f.path)),
                )
                for f in self.videofiles(handle.files)
            ]
            result.append(BtTorrentEntry(
                info_hash=handle.info_hash,
                title=handle.title,
                progress=handle.progress,
                files=files,
            ))
        return result

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
            if isinstance(message, BaseModel):
                message = message.model_dump(mode='json')
            elif isinstance(message, list):
                message = [m.model_dump(mode='json') if isinstance(m, BaseModel) else m for m in message]
            request['response'] = message
            await self.ws.send_json(request)

    def _validate(self, model_cls, data: dict):
        try:
            return model_cls.model_validate(data)
        except ValidationError as err:
            self.log.warning('invalid %s payload: %s', model_cls.__name__, err)
            return None

    async def onMessage(self, request: dict, action: str, data: dict) -> None:
        self.log.debug('onMessage action: %s', action)
        self._msg = request
        if action == 'transporturi':
            req = self._validate(TransportUriRequest, data)
            if req is None:
                return
            url = req.url
            if req.cookie:
                #TODO
                print("cookie", req.cookie)
                url = "http://{}:8080/?url={}&cookie={}".format(self.local, urllib.parse.quote(url), urllib.parse.quote(req.cookie))
            self.log.info('push to play relative %s, url: %s', req.relative, url)
            await self.hub.upnp.transporturi(url, req.title, req.relative)
        elif action == 'play':
            await self.hub.upnp.play()
        elif action == 'pause':
            await self.hub.upnp.pause()
        elif action == 'stop':
            await self.hub.upnp.stop()
        elif action == 'refresh':
            await self.hub.upnp.refresh()
        elif action == 'selectrenderer':
            req = self._validate(SelectRendererRequest, data)
            if req is None:
                return
            self.hub.upnp.select_device(req.udn)
        elif action == 'search':
            req = self._validate(UrlRequest, data)
            if req is None:
                return
            self.log.info('search %s', req.url)
            ret = await self.hub.info.youtube_dl(req.url)
            await self.sendMessage(ret)
        elif action == 'add':
            req = self._validate(UrlRequest, data)
            if req is None:
                return
            url = req.url
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
            req = self._validate(UrlRequest, data)
            if req is None:
                return
            self.hub.torrent.remove_torrent(req.url)
        elif action == 'load':
            req = self._validate(LoadRequest, data)
            if req is None:
                return
            self.hub.torrent.load_torrent(req.hash)
        elif action == 'btstatus':
            await self.sendMessage(self.btfileslist(self.hub.torrent.list_files()))
        elif action == 'upnpstatus':
            await self.sendMessage(self.hub.upnp.status)
        elif action == 'recheck':
            req = self._validate(UrlRequest, data)
            if req is None:
                return
            self.hub.torrent.recheck(req.url)
