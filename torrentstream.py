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


class DynamicTorrentProducer(static.StaticProducer):
    def __init__(self, stream, request, fileinfo, offset=0, size=None):
        self.stream = stream
        self.request = request
        self.fileinfo = fileinfo
        self.offset = offset
        self.size = size or fileinfo.info.size - offset
        self.lastoffset = self.offset + self.size
        self.priority_window = None

    def read_piece_alert(self, alert):
        print("read_piece_alert", alert.piece, alert.size)
        buffer = alert.buffer[self.piece.start:self.lastoffset - self.offset]
        self.request.write(buffer)
        self.offset += len(buffer)
        if self.offset < self.lastoffset:
            self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, self.size)
        #elif self.request:
        #    self.request.unregisterProducer()
        #    self.request.finish()
        #    self.stopProducing()

    def piece_finished_alert(self, alert):
        print("piece_finished_alert", alert.message())
        if alert.piece_index == self.priority_window:
            self.slide()

    def resumeProducing(self):
        print("index", self.piece.piece)
        self.fileinfo.handle.set_piece_deadline(self.piece.piece, 0, libtorrent.deadline_flags.alert_when_available)

    def stopProducing(self):
        print("stopProducing")
        self.stream.remove_alert_handler('read_piece_alert', self.read_piece_alert, self.fileinfo.handle)
        self.stream.remove_alert_handler('piece_finished_alert', self.piece_finished_alert, self.fileinfo.handle)

    def start(self):
        self.stream.add_alert_handler('read_piece_alert', self.read_piece_alert, self.fileinfo.handle)
        self.stream.add_alert_handler('piece_finished_alert', self.piece_finished_alert, self.fileinfo.handle)
        self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, self.size)
        self.lastpiece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.lastoffset, self.size)
        self.piecelength = self.fileinfo.handle.get_torrent_info().piece_length()

        # priority window size 4Mb * 8
        priorityblock = (4 * 1024 * 1024 )/ self.piecelength
        # piece_length more than 4Mb ?
        if priorityblock < 1:
            priorityblock = 1
        self.prioritymask = [ i for i in [TorrentStream.HIGHEST,TorrentStream.HIGHEST,6,5,4,3,2,1] for _ in range(priorityblock)]
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
            priority = 0
            for window in range(self.priority_window, min(self.lastpiece.piece + 1, self.priority_window + len(self.prioritymask))):
                self.fileinfo.handle.piece_priority(window, self.prioritymask[priority])
                priority += 1


class StaticTorrentProducer(DynamicTorrentProducer):
    def read_piece(self):
        if not hasattr(self, 'fileObject') or self.fileObject.closed:
            self.fileObject = open(self.fileinfo.handle.save_path() + self.fileinfo.info.path, 'rb')
            self.fileObject.seek(self.offset)

        data = self.fileObject.read(self.piecelength - self.piece.start)
        if data:
            self.offset += len(data)
            self.request.write(data)
        if self.offset < self.lastoffset:
            self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, self.size)

    def stopProducing(self):
        super(StaticTorrentProducer, self).stopProducing()
        self.fileObject.close()

    def resumeProducing(self):
        print("index", self.piece.piece)
        if self.fileinfo.handle.have_piece(self.piece.piece):
            self.read_piece()
        else:
            self.fileinfo.handle.set_piece_deadline(self.piece.piece, 0)

    def piece_finished_alert(self, alert):
        print("piece_finished_alert", alert.message())
        self.resumeProducing()
        if alert.piece_index == self.priority_window:
            self.slide()


class TorrentProducer(StaticTorrentProducer):
    pass

class TorrentStream(static.File):
    isLeaf = True
    PAUSE = 0
    LOW = 1
    NORMAL = 4
    HIGHEST = 7
    def __init__(self, **options):
        self._torrent_handlers = {}
        self._alert_handlers = {}
        self._files_list = {}
        self.options = options
        self.session = session = libtorrent.session()
        reactor.callInThread(self._alert_queue_loop)

        session.set_alert_mask(
                libtorrent.alert.category_t.tracker_notification |
                libtorrent.alert.category_t.storage_notification |
                libtorrent.alert.category_t.progress_notification |
                libtorrent.alert.category_t.status_notification |
                #libtorrent.alert.category_t.peer_notification |
                libtorrent.alert.category_t.error_notification
                )
        session.start_dht()
        session.start_lsd()
        session.start_upnp()
        session.start_natpmp()
        session.listen_on(options.get('min_port', 6881), options.get('max_port', 6889))

        session_settings = session.settings()
        session_settings.strict_end_game_mode = False
        session_settings.announce_to_all_tiers = True
        session_settings.announce_to_all_trackers = True
        session_settings.low_prio_disk = False
        session_settings.use_disk_cache_pool = True
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
            print('got {} files'.format(alert.handle.get_torrent_info().num_files()))
            self._torrent_handlers[str(alert.handle.info_hash())] = alert.handle

        def torrent_update_alert(alert):
            handle = self._torrent_handlers.pop(str(alert.old_ih), None)
            if handle:
                self._torrent_handlers[str(alert.new_ih)] = handle

        def torrent_checked_alert(alert):
            alert.handle.prioritize_pieces(alert.handle.get_torrent_info().num_pieces() * [TorrentStream.PAUSE])
            for i in range(alert.handle.get_torrent_info().num_files()):
                info = alert.handle.get_torrent_info().file_at(i)
                self._files_list[info.path] = FileInfo(id=i, handle=alert.handle, info=info)

        self.add_alert_handler('metadata_received_alert', metadata_received_alert)
        self.add_alert_handler('torrent_update_alert', torrent_update_alert)
        self.add_alert_handler('torrent_checked_alert', torrent_checked_alert)

    def _alert_queue_loop(self):
        print("_alert_queue_loop")
        while reactor.running:
            if not self.session.wait_for_alert(5000):
                continue
            reactor.callLater(0, self._handle_alert, self.session.pop_alerts())

    def _handle_alert(self, alerts):
        for alert in alerts:
            if alert.what() != 'block_finished_alert' and alert.what() != 'block_downloading_alert':
                print('{0}: {1}'.format(alert.what(), alert.message()))
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
        add_torrent_params['paused'] = False
        self.session.async_add_torrent(add_torrent_params)

    def remove_torrent(self, info_hash):
        handle = self._torrent_handlers.pop(info_hash, None)
        if handle:
            self.session.remove_torrent(handle, libtorrent.options_t.delete_files)
            return {'status': '{} removed'.format(info_hash)}
        return {'error': '{} not found'.format(info_hash)}

    def list_torrents(self):
        data = {}
        for info_hash, handler in self._torrent_handlers.items():
            data[info_hash] = []
            for file in handler.get_torrent_info().files():
                data[info_hash].append(file.path)
        return data

    def status(self):
        status = {}
        sst = self.session.status()
        status['dht_nodes'] = sst.dht_nodes
        status['is_dht_running'] = self.session.is_dht_running()
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
                file_map = ''
                for file_index in range(handle.get_torrent_info().num_files()):
                    file_map += str(handle.file_priority(file_index))
                s['fm'] = file_map
            s['m'] = handle.has_metadata()
            s['is_paused'] = handle.is_paused()
            st = handle.status()
            s['paused'] = st.paused
            s['state'] = st.state
            s['error'] = st.error
            s['progress'] = '{:.2%}'.format(st.progress)
            s['download_rate'] = st.download_rate
            s['upload_rate'] = st.upload_rate
            s['num_seeds'] = st.num_seeds
            s['num_complete'] = st.num_complete
            s['num_peers'] = st.num_peers
            s['num_incomplete'] = st.num_incomplete
            s['auto_managed'] = st.auto_managed
            s['trackers'] = handle.trackers()
            s['upload_mode'] = st.upload_mode
            status[info_hash] = s
        return status

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
        ret = None
        if request.postpath[0] == 'add' and url:
            self.add_torrent(url)
            ret = {'status': '{} added'.format(url)}
        elif request.postpath[0] == 'info':
            ret = self.status()
        elif request.postpath[0] == 'ls':
            ret = self._files_list.keys()
        elif request.postpath[0] == 'get' and url:
            if url not in self._files_list.keys():
                ret = {'error': '{} not found'.format(url)}
            else:
                self.type, self.encoding = static.getTypeAndEncoding(url,
                                              self.contentTypes,
                                              self.contentEncodings,
                                              "text/html")

                request.setHeader('accept-ranges', 'bytes')

                self.fileForReading = self._files_list[url]
                producer = self.makeProducer(request, self.fileForReading)
                producer.start()
                ret = server.NOT_DONE_YET
        elif request.postpath[0] == 'rm' and url:
            ret = self.remove_torrent(url)
        else:
            prepath = '{}:{}/{}'.format(request.host.host, request.host.port, '/'.join(request.prepath))
            ret = {'example': [ '{p}/add?url=http%3A%2F%2Fnewstudio.tv%2Fdownload.php%3Fid%3D17544'.format(p=prepath),
                                '{p}/get?url=file.avi'.format(p=prepath),
                                '{p}/rm?url=3bebb88255c4e3a2080b514a47a41fe75cbd8a40'.format(p=prepath),
                                '{p}/info'.format(p=prepath),
                                '{p}/ls'.format(p=prepath)
                              ]}

        return json.dumps(ret)


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
