#!/usr/bin/env python
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
# Media Renderer control server

from twisted.internet import reactor, threads
from twisted.web import server
from twisted.web.resource import Resource
from twisted.web.client import Agent
from twisted.web.http_headers import Headers
from autobahn.twisted.websocket import WebSocketServerFactory, WebSocketServerProtocol
from autobahn.twisted.resource import WebSocketResource
from coherence.base import Coherence
from coherence.upnp.devices.control_point import ControlPoint
from coherence.upnp.core import DIDLLite
import json
import urllib
import torrentstream
import logging
import logging.handlers

def printall(*args, **kwargs):
    print("printall", args, kwargs)

class MediaDevice(object):
    def __init__(self, device):
        self.media = device
        self.status = {'state': None, 'item': [], 'device': device.get_friendly_name()}

    def __repr__(self):
        return "{} {} {}".format(device, status)

class UPnPctrl(object):
    def __init__(self):
        self.coherence = Coherence({'logmode':'warning'})
        self.control_point = ControlPoint(self.coherence, auto_client=['MediaRenderer'])
        #self.control_point.connect(self.media_renderer_found, 'Coherence.UPnP.ControlPoint.MediaRenderer.detected')
        #self.control_point.connect(self.media_renderer_removed, 'Coherence.UPnP.ControlPoint.MediaRenderer.removed')
        self.control_point.connect(self.media_renderer_found, 'Coherence.UPnP.RootDevice.detection_completed')
        self.control_point.connect(self.media_renderer_removed, 'Coherence.UPnP.RootDevice.removed')
        #self.control_point.connect(printall, 'Coherence.UPnP.RootDevice.detection_completed')
        #self.control_point.connect(printall, 'Coherence.UPnP.RootDevice.removed')

        self.mediadevices = {}
        self.device = None
        self.registered_callbacks = {}

    def media_renderer_removed(self, usn = None):
        print("media_renderer_removed", usn)
        self.mediadevices.pop(usn, None)
        if self.device:
            if self.device.media.get_usn() == usn:
                if len(self.mediadevices):
                    self.device = list(self.mediadevices.values())[0]
                else:
                    self.device = None
                self.trigger_callbacks()

    def media_renderer_found(self, device = None):
        if device is None:
            return

        print("found upnp device", device.get_usn(), device.get_friendly_name())

        if device.get_device_type().find('MediaRenderer') < 0:
            return

        print("media renderer", device.get_friendly_name())

        mediadevice = MediaDevice(device)
        self.mediadevices[device.get_usn()] = mediadevice
        if not self.device:
            self.device = mediadevice
            self.trigger_callbacks()

        device.client.av_transport.subscribe_for_variable('CurrentTrackMetaData', self.state_variable_change)
        device.client.av_transport.subscribe_for_variable('TransportState', self.state_variable_change)
 
    def play(self, url, title='Video', vtype='video/mp4'):
        if self.device:
            def handle_response(response):
                ctype = response.headers.getRawHeaders('content-type', default=[vtype])[0]
                print("type", ctype)
                mime = 'http-get:*:%s:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000' % ctype
                res = DIDLLite.Resource(url, mime)
                item = DIDLLite.VideoItem(None, None, None)
                item.title = title
                item.res.append(res)
                didl = DIDLLite.DIDLElement()
                didl.addItem(item)
                service = self.device.media.get_service_by_type('AVTransport')
                transport_action = service.get_action('SetAVTransportURI')
                stop_action = service.get_action('Stop')
                play_action = service.get_action('Play')
                d = stop_action.call(InstanceID=0)
                d.addBoth(lambda _: transport_action.call(InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl.toString()))
                d.addCallback(lambda _: play_action.call(InstanceID=0, Speed=1))
                d.addErrback(printall)
            agent = Agent(reactor)
            d = agent.request('HEAD', url.encode(), None)
            d.addCallback(handle_response)
            d.addErrback(printall)

    def add_alert_handler(self, callback):
        self.registered_callbacks[id(callback)] = {'status': None, 'callback': callback}
        self.trigger_callbacks()

    def remove_alert_handler(self, callback):
        self.registered_callbacks.pop(id(callback), None)
            
    def state_variable_change(self, variable):
        usn = variable.service.device.get_usn()
        if variable.name == 'CurrentTrackMetaData':
            if variable.value != None and len(variable.value)>0:
                try:
                    elt = DIDLLite.DIDLElement.fromString(variable.value)
                    self.mediadevices[usn].status['item'] = []
                    for item in elt.getItems():
                        print("now playing:", item.title, item.id)
                        self.mediadevices[usn].status['item'].append({'url':item.id, 'title':item.title})
                except SyntaxError:
                    return
        elif variable.name == 'TransportState':
            print(variable.name, 'changed from', variable.old_value, 'to', variable.value)
            self.mediadevices[usn].status['state'] = variable.value
        self.trigger_callbacks()

    def trigger_callbacks(self):
        for callback in self.registered_callbacks.values():
            try:
                status = self.device.status if self.device is not None else None
                if callback['status'] != status:
                    callback['callback'](status)
                    if status:
                        callback['status'] = status.copy()
                    else:
                        callback['status'] = status
            except Exception as e:
                print("trigger_callbacks exception", e)

    def refresh(self):
        self.coherence.msearch.double_discover()

class JSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, "__json__"):
            return obj.__json__()
        elif isinstance(obj, bytes):
            return obj.decode("utf8", "ignore")
        else:
            return json.JSONEncoder.default(self, obj)

import youtube_dl
#delete generic extractor
youtube_dl.extractor._ALL_CLASSES.pop()

class Info(Resource):
    @staticmethod
    def livestreamer(url):
        import livestreamer
        try:
            ls = livestreamer.Livestreamer()
            plugin = ls.resolve_url(url)
            stream = plugin.streams()
            return stream
        except livestreamer.exceptions.NoPluginError as e:
            return "livestreamer.exceptions.NoPluginError {}".format(e)
        except AttributeError as e:
            return "exceptions.AttributeError {}".format(e)

    @staticmethod
    def youtube_dl(url):
        #try:
            ydl = youtube_dl.YoutubeDL(
                params={
                    'quiet': True,
                    'cachedir': '/tmp/',
                    'youtube_include_dash_manifest': False,
                    'prefer_ffmpeg': True
                })
            stream = ydl.extract_info(url, download=False, process=True)
            #format_selector = ydl.build_format_selector('all[height>=480]')
            #select = list(format_selector(stream.get('formats')))
            data = {}
            data['src'] = stream.get('extractor')
            data['title'] = stream.get('title')
            data['url'] = stream.get('webpage_url')
            data['bitrate'] = []
            for i in stream.get('formats',[]):
                if i.get('acodec') != 'none' and i.get('vcodec') != 'none':
                    data['bitrate'].append({'url':i.get('url'), 'bitrate':i.get('height') or i.get('format_id')})
            return data
        #except youtube_dl.utils.DownloadError as e:
        #    return None

    def render_GET(self, request):
        url = request.args.get('url',[None])[0]
        if url:
            #d = threads.deferToThread(self.livestreamer, url)
            d = threads.deferToThread(self.youtube_dl, url)
            d.addCallback(lambda data: (request.write(json.dumps(data, cls=JSONEncoder, indent=2)), request.finish()))
            d.addErrback(lambda data: (request.write('plugin callback error: {}'.format(data)), request.finish()))
            return server.NOT_DONE_YET
        return "no 'url' parameter pecified"

class Play(Resource):
    def render_GET(self, request):
        url = request.args.get('url',[None])[0]
        if url:
            print("push to play url:", request.args.get('url'))
            upnp.play(url, request.args.get('title', 'Video'))
            return 'play'
        return "no 'url' parameter pecified"

class Root(Resource):
    def getChild(self, path, request):
        return self

    def render_GET(self, request):
        prepath = 'index.html'
        if len(request.prepath) == 1 and request.prepath[0] == 'popup.css':
            prepath = request.prepath[0]
            request.setHeader("Content-Type", "text/css; charset=utf-8")
        with open(prepath, 'r') as f:
            return f.read()

class WS(WebSocketServerProtocol):
    @staticmethod
    def videofiles(files):
        return [i for i in files if torrentstream.TorrentStream.getTypeAndEncoding(i)[0].startswith('video')]

    def btfileslist(self, infiles):
        import socket
        files = self.videofiles(infiles)
        return {
                 'prefix':'http://{}:{}/bt/get?url='.format(socket.gethostbyname(self.http_request_host), '8880'),
                 'files':files
               }

    def onOpen(self):
        def upnpupdate(message):
            self.sendMessage({'action':'upnpupdate', 'upnpupdate':message})

        def btupdate(alert):
            files = self.videofiles(alert.files)
            self.sendMessage({'action':'btupdate', 'btupdate': self.btfileslist(alert.files)})

        # handle function id must be same on adding and removing alert
        self._upnpupdate = upnpupdate
        self._btupdate = btupdate
        print("WS client connected", self.peer)
        upnp.add_alert_handler(self._upnpupdate)
        torrent.add_alert_handler('files_list_update_alert', self._btupdate)

    def onClose(self, wasClean, code, reason):
        if hasattr(self, '_upnpupdate'):
            print("WS client closed", reason , id(self._upnpupdate))
            upnp.remove_alert_handler(self._upnpupdate)
        if hasattr(self, '_btupdate'):
            torrent.remove_alert_handler('files_list_update_alert', self._btupdate)

    def sendMessage(self, message, request = {}):
        uid = request.get('_uid', None)
        if uid:
            response = {'_uid': uid, 'data': message}
        else:
            response = message
        super(WS, self).sendMessage(json.dumps(response))

    def onMessage(self, payload, isBinary):
        print("WS onMessage", payload)
        jsondata = json.loads(payload)
        if jsondata.get('action') == 'play':
            data = jsondata.get('play', {})
            url = data.get('url')
            if url:
                if data.get('cookie'):
                    print("cookie", data.get('cookie'))
                    url = "http://{}:8080/?url={}&cookie={}".format(self.http_request_host, urllib.quote(url), urllib.quote(data.get('cookie')))
                print("push to play url:", url)
                upnp.play(url, data.get('title', 'Video'))
        elif jsondata.get('action') == 'refresh':
            upnp.refresh()
        elif jsondata.get('action') == 'search':
            data = jsondata.get('search')
            url = data.get('url')
            print('search', url)
            def jsonsend(message):
                self.sendMessage(message, jsondata)
            def errorsend(message):
                self.sendMessage(None, jsondata)
            d = threads.deferToThread(Info.youtube_dl, url)
            d.addCallback(jsonsend)
            d.addErrback(errorsend)
        elif jsondata.get('action') == 'add':
            data = jsondata.get('add')
            url = data.get('url')
            print('add', url)
            def jsonsend(message):
                self.sendMessage(message, jsondata)
            def bittorrent(message):
                self.sendMessage(None, jsondata)
                torrent.add_torrent(url)
            d = threads.deferToThread(Info.youtube_dl, url)
            d.addCallback(jsonsend)
            d.addErrback(bittorrent)
        elif jsondata.get('action') == 'btstatus':
            self.sendMessage(self.btfileslist(torrent.list_files()), jsondata)
        elif jsondata.get('action') == 'upnpstatus':
            message = upnp.device.status if upnp.device else None
            self.sendMessage(message, jsondata)

upnp = UPnPctrl()
torrent = torrentstream.TorrentStream(save_path='/media/sda/tmp/')

def start():
    root = Root()
    root.putChild("info", Info())
    root.putChild("play", Play())
    root.putChild("bt", torrent)
    ws = WebSocketServerFactory()
    ws.protocol = WS
    reactor.listenTCP(8881, ws)
#    root.putChild("ws", WebSocketResource(ws))

    site = server.Site(root)
    reactor.listenTCP(8880, site)

reactor.callWhenRunning(start)
reactor.run()

