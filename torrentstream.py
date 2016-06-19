#!/usr/bin/env python
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
# Torrent Stream based on libtorrent

import libtorrent
import json
from twisted.internet import reactor
from twisted.web import server, static, http
from twisted.web.resource import Resource
from collections import namedtuple

FileInfo = namedtuple('FileInfo', ('id', 'handle', 'info'))


class TorrentProducer(static.StaticProducer):
    def __init__(self, stream, request, fileinfo, offset=0, size=None):
        self.stream = stream
        self.request = request
        self.fileinfo = fileinfo
        self.offset = offset
        self.size = size or fileinfo.info.size
        self.priority_window = None

    def read_piece_alert(self, alert):
        print("read_piece_alert", alert.piece, alert.size)
        buffer = alert.buffer[self.piece.start:self.size - self.offset]
        self.request.write(buffer)
        self.offset += len(buffer)
        if self.offset < self.size:
            self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, self.size)

    def piece_finished_alert(self, alert):
        print("piece_finished_alert", alert.message())
        if alert.piece_index == self.piece.piece:
            alert.handle.read_piece(self.piece.piece)
        if alert.piece_index == self.priority_window:
            self.slide()

    def resumeProducing(self):
        print("index", self.piece.piece)
        if self.fileinfo.handle.have_piece(self.piece.piece):
            self.fileinfo.handle.read_piece(self.piece.piece)

    def stopProducing(self):
        print("stopProducing")
        self.stream.remove_alert_handler('read_piece_alert', self.read_piece_alert, self.fileinfo.handle)
        self.stream.remove_alert_handler('piece_finished_alert', self.piece_finished_alert, self.fileinfo.handle)
        self.fileinfo.handle.file_priority(self.fileinfo.id, TorrentStream.PAUSE)

    def start(self):
        self.stream.add_alert_handler('read_piece_alert', self.read_piece_alert, self.fileinfo.handle)
        self.stream.add_alert_handler('piece_finished_alert', self.piece_finished_alert, self.fileinfo.handle)
        self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, self.size)
        self.lastpiece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.size, self.size)
#        for i in range(self.firstpiece.piece, self.lastpiece.piece):
#            self.fileinfo.handle.piece_priority(i, TorrentStream.PAUSE)
#        self.fileinfo.handle.file_priority(self.fileinfo.id, TorrentStream.NORMAL)
        self.slide(self.piece.piece)
        self.request.registerProducer(self, 0)

    def slide(self, offset = None):
        if offset is not None:
            self.priority_window = offset
        # find next missing piece from current piece offset
        while self.fileinfo.handle.have_piece(self.priority_window) and self.priority_window <= self.lastpiece.piece:
            self.priority_window += 1
        print("priority_window", self.priority_window)
        if self.priority_window <= self.lastpiece.piece:
            # set next eight pieces priority to 77654321
            self.fileinfo.handle.piece_priority(self.priority_window, TorrentStream.HIGHEST)
            priority = TorrentStream.HIGHEST
            for window in range(self.priority_window + 1, min(self.lastpiece.piece + 1, self.priority_window + TorrentStream.HIGHEST + 1)):
                self.fileinfo.handle.piece_priority(window, priority)
                priority -= 1


class TorrentStream(static.File):
    isLeaf = True
    PAUSE = 0
    NORMAL = 1
    HIGHEST = 7
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

        def metadata_received_alert(alert):
            print('pause {} files'.format(alert.handle.get_torrent_info().num_files()))
            alert.handle.prioritize_files(alert.handle.get_torrent_info().num_files() * [TorrentStream.PAUSE])

        def torrent_checked_alert(alert):
            for i in range(alert.handle.get_torrent_info().num_files()):
                info = alert.handle.get_torrent_info().file_at(i)
                self._files_list[info.path] = FileInfo(id=i, handle=alert.handle, info=info)

        def torrent_added_alert(alert):
            self._torrent_handlers[str(alert.handle.info_hash())] = alert.handle
            alert.handle.resume()

        self.add_alert_handler('metadata_received_alert', metadata_received_alert)
        self.add_alert_handler('torrent_added_alert', torrent_added_alert)
        self.add_alert_handler('torrent_checked_alert', torrent_checked_alert)

    def _alert_queue_loop(self):
        print("_alert_queue_loop")
        while reactor.running:
            if not self.session.wait_for_alert(500):
                continue
            reactor.callLater(0, self._handle_alert, self.session.pop_alerts())

    def _handle_alert(self, alerts):
        for alert in alerts:
            #if alert.what() != 'block_finished_alert' and alert.what() != 'block_downloading_alert':
            #    print('{0}: {1}'.format(alert.what(), alert.message()))
            if alert.what() in self._alert_handlers:
                for handler in self._alert_handlers[alert.what()]:
                    handler(alert)
            if hasattr(alert, 'handle'):
                what = str(alert.handle.info_hash()) + ':' + alert.what()
                for handler in self._alert_handlers.get(what, []):
                    handler(alert)

    def add_alert_handler(self, alert, handler, handle=None):
        if handle:
            alert = str(handle.info_hash()) + ':' + alert
        if handler not in self._alert_handlers.setdefault(alert, []):
            self._alert_handlers[alert].append(handler)

    def remove_alert_handler(self, alert, handler, handle=None):
        if handle:
            alert = str(handle.info_hash()) + ':' + alert
        if alert in self._alert_handlers and handler in self._alert_handlers[alert]:
            self._alert_handlers[alert].remove(handler)
            if not self._alert_handlers[alert]:
                self._alert_handlers.pop(alert)

    def add_torrent(self, url):
        add_torrent_params = {}
        add_torrent_params['url'] = url
        add_torrent_params['save_path'] = self.options.get('save_path', '/tmp/')
        add_torrent_params['storage_mode'] = libtorrent.storage_mode_t.storage_mode_sparse
        add_torrent_params['auto_managed'] = False
        self.session.async_add_torrent(add_torrent_params)

    def list_torrents(self):
        data = {}
        for info_hash, handler in self._torrent_handlers.items():
            data[info_hash] = []
            for file in handler.get_torrent_info().files():
                data[info_hash].append(file.path)
        return data

    def getFileSize(self):
        return self.fileForReading.info.size

    def makeProducer(self, request, fileForReading):
        byteRange = request.getHeader(b'range')
        if byteRange is None:
            self._setContentHeaders(request)
            request.setResponseCode(http.OK)
            return TorrentProducer(self, request, fileForReading)
        try:
            parsedRanges = self._parseRangeHeader(byteRange)
        except ValueError:
            self._setContentHeaders(request)
            request.setResponseCode(http.OK)
            return TorrentProducer(self, request, fileForReading)

        if len(parsedRanges) == 1:
            offset, size = self._doSingleRangeRequest(
                request, parsedRanges[0])
            self._setContentHeaders(request, size)
            return TorrentProducer(
                self, request, fileForReading, offset, size)
        else:
            rangeInfo = self._doMultipleRangeRequest(request, parsedRanges)
            return TorrentProducer(
                self, request, fileForReading, rangeInfo)

    def render_GET(self, request):
        url = request.args.get('url',[None])[0]
        if request.postpath[0] == 'add' and url:
            self.add_torrent(url)
            return '{"status": "ok"}'
        elif request.postpath[0] == 'info':
            print("alert_handlers", self._alert_handlers)
            status = {}
            for info_hash, handle in self._torrent_handlers.items():
                s = {}
                if handle.has_metadata():
                    piece_map = ''
                    for piece_index in range(handle.get_torrent_info().num_pieces()):
                        if handle.have_piece(piece_index):
                            piece_map += '*'
                        else:
                            piece_map += str(handle.piece_priority(piece_index))
                    s['pm'] = piece_map
                s['m'] = handle.has_metadata()
                st = handle.status()
                s['paused'] = st.paused
                s['state'] = st.state
                s['progress'] = st.progress
                status[info_hash] = s
            return json.dumps(status)
            #return json.dumps(self.list_torrents())
        elif request.postpath[0] == 'ls':
            return json.dumps(self._files_list.keys())
        elif request.postpath[0] == 'get' and url:
            self.type, self.encoding = static.getTypeAndEncoding(url,
                                              self.contentTypes,
                                              self.contentEncodings,
                                              "text/html")

            if url not in self._files_list.keys():
                return self.childNotFound.render(request)

            request.setHeader('accept-ranges', 'bytes')

            self.fileForReading = self._files_list[url]
            producer = self.makeProducer(request, self.fileForReading)
            producer.start()
            return server.NOT_DONE_YET

        return '{"status": "unknown url"}'


def main():
    print(libtorrent.version)
    root = Resource()
    torrentstream = TorrentStream(save_path='/media/sda/tmp/')
    #torrentstream = TorrentStream()
    root.putChild("bt", torrentstream)
    site = server.Site(root)
    reactor.listenTCP(8881, site)

if __name__ == '__main__':
    reactor.callWhenRunning(main)
    reactor.run()
