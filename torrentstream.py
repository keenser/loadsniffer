#!/usr/bin/env python
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
# Torrent Stream based on libtorrent

import libtorrent
import json
from twisted.internet import reactor, threads
from twisted.web import server, static
from twisted.web.resource import Resource
from collections import namedtuple
import io

FileInfo = namedtuple('FileInfo', ('id', 'handler', 'info'))

class FileReader(object):
    def __init__(self, stream, fileinfo):
        self.stream = stream
        self.fileinfo = fileinfo
        self.offset = None

    def seek(self, offset, whence=io.SEEK_SET):
        self.offset = offset

    def read(self, lengh):
        pass

    def close(self):
        pass

class TorrentStream(static.File):
    isLeaf = True
    def __init__(self, **options):
        self._torrent_handlers = {}
        self._alert_handlers = {}
        self._files_list = {}
        self.options = options
        self.session = session = libtorrent.session()

        session.set_alert_mask(
                libtorrent.alert.category_t.tracker_notification |
                libtorrent.alert.category_t.storage_notification |
                libtorrent.alert.category_t.progress_notification |
                libtorrent.alert.category_t.status_notification |
                libtorrent.alert.category_t.error_notification |
                libtorrent.alert.category_t.peer_notification
                )
        session.start_dht()
        session.start_lsd()
        session.start_upnp()
        session.start_natpmp()
        session.listen_on(options.get('min_port', 6881), options.get('max_port', 6889))
        reactor.callInThread(self._alert_queue_loop)

        session_settings = session.settings()
        session_settings.strict_end_game_mode = False
        session_settings.announce_to_all_tiers = True
        session_settings.announce_to_all_trackers = True
        session.set_settings(session_settings)

        session.add_dht_router("router.bittorrent.com", 6881)
        session.add_dht_router("router.utorrent.com", 6881)

        encryption_settings = libtorrent.pe_settings()
        encryption_settings.out_enc_policy = libtorrent.enc_policy(libtorrent.enc_policy.forced)
        encryption_settings.in_enc_policy = libtorrent.enc_policy(libtorrent.enc_policy.forced)
        encryption_settings.allowed_enc_level = libtorrent.enc_level.both
        encryption_settings.prefer_rc4 = True
        session.set_pe_settings(encryption_settings)

        def process_files(alert):
            print('pause {} files'.format(alert.handle.get_torrent_info().num_files()))
            alert.handle.prioritize_files(alert.handle.get_torrent_info().num_files() * [0])
            for i in range(alert.handle.get_torrent_info().num_files()):
                info = alert.handle.get_torrent_info().file_at(i)
                self._files_list[info.path] = FileInfo(handler=alert.handle, info=info)
        self.add_alert_handler('metadata_received_alert', process_files)

    def _alert_queue_loop(self):
        print("_alert_queue_loop")
        while reactor.running:
            if not self.session.wait_for_alert(1000):
                continue
            reactor.callLater(0, self._handle_alert, self.session.pop_alerts())

    def _handle_alert(self, alerts):
        for alert in alerts:
            print('{0}: {1}'.format(alert.what(), alert.message()))
            if alert.what() in self._alert_handlers:
                for handler in self._alert_handlers[alert.what()]:
                    handler(alert)

    def add_alert_handler(self, alert, handler):
        if handler not in self._alert_handlers.setdefault(alert,[]):
            self._alert_handlers[alert].append(handler)

    def remove_alert_handler(self, alert, handler):
        if alert in self._alert_handlers and handler in self._alert_handlers[alert]:
                self._alert_handlers[alert].remove(handler)

    def add_torrent(self, url):
        add_torrent_params = {}
        add_torrent_params['url'] = url
        add_torrent_params['save_path'] = self.options.get('save_path', '/tmp/')
        add_torrent_params['storage_mode'] = libtorrent.storage_mode_t.storage_mode_sparse
        add_torrent_params['auto_managed'] = False
        self._torrent_handlers[url] = self.session.add_torrent(add_torrent_params)
        self._torrent_handlers[url].resume()

    def list_torrents(self):
        data = {}
        for url, handler in self._torrent_handlers.items():
            data[url] = []
            for file in handler.get_torrent_info().files():
                data[url].append(file.path)
        return data

    def openForReading(self, url):
        return FileReader(self, self._files_list[url])

    def render_GET(self, request):
        print("get", request)
        url = request.args.get('url',[None])[0]
        if request.postpath[0] == 'add' and url:
            self.add_torrent(url)
            return '{"status": "ok"}'
        elif request.postpath[0] == 'info':
            return json.dumps(self.list_torrents())
        elif request.postpath[0] == 'ls':
            return json.dumps(self._files_list.keys())
            #return json.dumps(item for sublist in self.list_torrents().values() for item in sublist)
        elif request.postpath[0] == 'get' and url:
            request.setHeader('accept-ranges', 'bytes')
            try:
                fileForReading = self.openForReading(url)
            except IOError, e:
                return '{"status": "{}"}'.format(e)
            producer = self.makeProducer(request, fileForReading)
            url = request.args.get('url')[0]
            producer.start()
            return server.NOT_DONE_YET

        return '{"status": "unknown url"}'
    render_HEAD = render_GET

def main():
    root = Resource()
    torrentstream = TorrentStream(save_path='/media/sda/tmp/')
    root.putChild("bt", torrentstream)
    site = server.Site(root)
    reactor.listenTCP(8881, site)

if __name__ == '__main__':
    reactor.callWhenRunning(main)
    reactor.run()
