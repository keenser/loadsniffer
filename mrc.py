#!/usr/bin/env python
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
# Media Renderer control server

from twisted.internet import reactor, threads
from twisted.web import server
from twisted.web.resource import Resource
from autobahn.twisted.websocket import WebSocketServerFactory, WebSocketServerProtocol
from autobahn.twisted.resource import WebSocketResource
from coherence.base import Coherence
from coherence.upnp.devices.control_point import ControlPoint
from coherence.upnp.core import DIDLLite
import json
import urllib
import torrentstream

def printall(*args, **kwargs):
    print args, kwargs

class MediaDevice(object):
    def __init__(self, device):
        self.media = device
        self.status = {'state': None, 'item': []}

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
        print "media_renderer_removed", usn
        self.mediadevices.pop(usn, None)
        if self.device:
            if self.device.media.get_usn() == usn:
                if len(self.mediadevices):
                    self.device = list(self.mediadevices.values())[0]
                else:
                    self.device = None

    def media_renderer_found(self, device = None):
        print "media_renderer_found", device.get_usn(), device.get_friendly_name()
        if device is None:
            return

        if device.get_device_type().find('MediaRenderer') < 0:
            return

        mediadevice = MediaDevice(device)
        self.mediadevices[device.get_usn()] = mediadevice
        if not self.device:
            self.device = mediadevice
            self.trigger_callbacks()

        device.client.av_transport.subscribe_for_variable('CurrentTrackMetaData', self.state_variable_change)
        device.client.av_transport.subscribe_for_variable('TransportState', self.state_variable_change)
 
    def play(self, url, title='Video', vtype='video/avi'):
        if self.device:
            mime = 'http-get:*:%s:*' % vtype
            res = DIDLLite.Resource(url, mime)
            item = DIDLLite.VideoItem(None, None, None)
            item.title = title
            item.res.append(res)
            didl = DIDLLite.DIDLElement()
            didl.addItem(item)
            service = self.device.media.get_service_by_type('AVTransport')
            transport_action= service.get_action('SetAVTransportURI')
            play_action = service.get_action('Play')
            d = transport_action.call(InstanceID=0, CurrentURI='url', CurrentURIMetaData=didl.toString())
            d.addCallback(lambda x: play_action.call(InstanceID=0, Speed=1))
            d.addErrback(printall)

    def on_status_change(self, status, callback):
        if status:
            self.registered_callbacks[id(callback)] = {'status': status, 'callback': callback}
            self.trigger_callbacks()
        else:
            self.registered_callbacks.pop(id(callback), None)
            
    def state_variable_change(self, variable):
        usn = variable.service.device.get_usn()
        if variable.name == 'CurrentTrackMetaData':
            if variable.value != None and len(variable.value)>0:
                try:
                    elt = DIDLLite.DIDLElement.fromString(variable.value)
                    self.mediadevices[usn].status['item'] = []
                    for item in elt.getItems():
                        print "now playing:", item.title, item.id
                        self.mediadevices[usn].status['item'].append({'url':item.id, 'title':item.title})
                except SyntaxError:
                    return
        elif variable.name == 'TransportState':
            print variable.name, 'changed from', variable.old_value, 'to', variable.value
            self.mediadevices[usn].status['state'] = variable.value
        self.trigger_callbacks()

    def trigger_callbacks(self):
        if self.device:
            for callback in self.registered_callbacks.values():
                try:
                    if callback['status'] != self.device.status:
                        callback['callback'](self.device.status)
                except Exception as e:
                    print "trigger_callbacks exception", e

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
#delete generid extractor
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
            ydl = youtube_dl.YoutubeDL(params={'quiet': True, 'cachedir': '/tmp/', 'youtube_include_dash_manifest': False})
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
            print "push to play url:", request.args.get('url')
            upnp.play(request.args.get('url'), request.args.get('title', 'Video'))
            return 'play'
        return "no 'url' parameter pecified"

class WS(WebSocketServerProtocol):
    def onOpen(self):
        def upnpupdate(message):
            self.sendMessage(json.dumps({'action':'upnp', 'upnp':message}))

        def btupdate(alert):
            files = [i for i in alert.files if i.endswith('.mkv') or i.endswith('.mp4') or i.endswith('.avi')]
            self.sendMessage(json.dumps({'action':'bt', 'bt':{'prefix':'http://192.168.1.19:8880/bt/get?url=', 'files':files}}))

        self._upnpupdate = upnpupdate
        self._btupdate = btupdate
        print "WS client connected", id(self._upnpupdate)
        upnp.on_status_change(True, self._upnpupdate)
        torrent.add_alert_handler('files_list_update_alert', self._btupdate)

    def onClose(self, wasClean, code, reason):
        print "WS client closed", reason , id(self._upnpupdate)
        upnp.on_status_change(None, self._upnpupdate)
        torrent.remove_alert_handler('files_list_update_alert', self._btupdate)

    def onMessage(self, payload, isBinary):
        jsondata = json.loads(payload)
        if jsondata.get('action') == 'play':
            data = jsondata['play']
            if data.get('url', None):
                print "play url", data.get('url')
                print "cookie", data.get('cookie')
                if data.get('cookie'):
                    url = "http://192.168.1.19:8080/?url={}&cookie={}".format(urllib.quote(data.get('url')), urllib.quote(data.get('cookie')))
                else:
                    url = data.get('url')
                print "push to play url:", url
                upnp.play(url, data.get('title', 'Video'))
        elif jsondata.get('action') == 'refresh':
            upnp.refresh()
        elif jsondata.get('action') == 'info':
            uid = jsondata.get('_uid')
            data = jsondata.get('info')
            url = data['url']
            print "info", url
            def jsonsend(message):
                self.sendMessage(json.dumps({'_uid': uid, 'data': message}))
            def bittorrent(message):
                torrent.add_torrent(url)
                jsonsend(None)
            d = threads.deferToThread(Info.youtube_dl, url)
            d.addCallback(jsonsend)
            d.addErrback(bittorrent)
        elif jsondata.get('action') == 'bt':
            uid = jsondata.get('_uid')
            filelist = torrent.list_files()
            files = [i for i in filelist if i.endswith('.mkv') or i.endswith('.mp4') or i.endswith('.avi')]
            message = {'prefix':'http://192.168.1.19:8880/bt/get?url=', 'files':files}
            self.sendMessage(json.dumps({'_uid': uid, 'data': message}))

upnp = UPnPctrl()
torrent = torrentstream.TorrentStream(save_path='/media/sda/tmp/')

def start():
    root = Resource()
    root.putChild("info", Info())
    root.putChild("play", Play())
    root.putChild("bt", torrent)
    ws = WebSocketServerFactory()
    ws.protocol = WS
    root.putChild("ws", WebSocketResource(ws))

    site = server.Site(root)
    reactor.listenTCP(8880, site)

reactor.callWhenRunning(start)
reactor.run()

