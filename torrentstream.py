#!/usr/bin/env python
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
# Torrent Stream based on libtorrent

import libtorrent
import json
import glob
import os
from twisted.internet import reactor, defer
from twisted.web import server, static, http
from twisted.web.resource import Resource
from collections import namedtuple
import gc

FileInfo = namedtuple('FileInfo', ('id', 'handle', 'info'))


class DynamicTorrentProducer(static.StaticProducer):
    def __init__(self, stream, request, fileinfo, offset=0, size=None):
        print("DynamicTorrentProducer", offset, size)
        self.stream = stream
        self.request = request
        self.fileinfo = fileinfo
        self.offset = offset
        self.size = size or fileinfo.info.size - offset
        self.lastoffset = self.offset + self.size - 1
        self.priority_window = None
        self.buffer = {}
        self.cansend = True

    def read_piece_alert(self, alert):
        print("read_piece_alert", alert.piece, alert.size)
        self.buffer[alert.piece] = alert.buffer
        if self.cansend:
            self.resumeProducing()

    def read_piece(self):
        print("read_piece", self.piece.start, self.piece.start + self.lastoffset - self.offset)
        buffer = self.buffer[self.piece.piece][self.piece.start:self.piece.start + self.lastoffset - self.offset]
        self.request.write(buffer)
        self.offset += len(buffer)
        del self.buffer[self.piece.piece]

        if self.offset < self.lastoffset:
            # move to next piece
            self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, 0)
        elif self.request:
            self.request.unregisterProducer()
            self.request.finish()
            self.stopProducing()

    def piece_finished_alert(self, alert):
        if self.cansend:
            self.resumeProducing()
        self.slide()

    def resumeProducing(self):
        print("index", self.piece.piece)
        for window in range(self.piece.piece, min(self.lastpiece.piece + 1, self.piece.piece + len(self.prioritymask))):
            if not window in self.buffer and self.fileinfo.handle.have_piece(window):
                self.buffer[window] = None
                self.fileinfo.handle.read_piece(window)
                #self.fileinfo.handle.set_piece_deadline(window, 0, libtorrent.deadline_flags.alert_when_available)
        if self.piece.piece in self.buffer and self.buffer[self.piece.piece]:
            self.cansend = False
            self.read_piece()
        else:
            self.cansend = True
        #self.fileinfo.handle.set_piece_deadline(self.piece.piece, 0, libtorrent.deadline_flags.alert_when_available)

    def stopProducing(self):
        print("stopProducing")
        self.stream.remove_alert_handler('read_piece_alert', self.read_piece_alert, self.fileinfo.handle)
        self.stream.remove_alert_handler('piece_finished_alert', self.piece_finished_alert, self.fileinfo.handle)

    def start(self):
        self.stream.add_alert_handler('read_piece_alert', self.read_piece_alert, self.fileinfo.handle)
        self.stream.add_alert_handler('piece_finished_alert', self.piece_finished_alert, self.fileinfo.handle)
        self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, 0)
        self.lastpiece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.lastoffset, 0)
        self.piecelength = self.fileinfo.handle.get_torrent_info().piece_length()
        print("start", self.piece.piece, self.lastpiece.piece, self.piecelength)

        # priority window size 4Mb * 8
        priorityblock = (4 * 1024 * 1024 )/ self.piecelength
        # piece_length more than 4Mb ?
        if priorityblock < 1:
            priorityblock = 1
        self.prioritymask = [ i for i in [TorrentStream.HIGHEST,TorrentStream.HIGHEST,6,5,4,3,2,1] for _ in range(priorityblock)]
        print("prioritymask", self.prioritymask)

        self.fileinfo.handle.resume()
        self.slide(self.piece.piece)

        self.request.registerProducer(self, 0)

    def slide(self, offset = None):
        if offset is not None:
            self.priority_window = offset
        window = self.priority_window
        for priority in self.prioritymask:
            while True:
                if window > self.lastpiece.piece:
                    return
                if self.fileinfo.handle.have_piece(window):
                    if window == self.priority_window:
                        self.priority_window += 1
                    window += 1
                else:
                    self.fileinfo.handle.set_piece_deadline(window, 0)
                    self.fileinfo.handle.piece_priority(window, priority)
                    window += 1
                    break


# speedup reading pieces using direct access to file on filesystem
class StaticTorrentProducer(DynamicTorrentProducer):
    def read_piece_1(self):
        with open(self.fileinfo.handle.save_path() + self.fileinfo.info.path, 'rb') as fileObject:
            fileObject.seek(self.offset)
            data = fileObject.read(self.piecelength - self.piece.start)

            if data:
                self.offset += len(data)
                self.request.write(data)
            if self.offset < self.lastoffset:
                self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, 0)
            elif self.request:
                self.request.unregisterProducer()
                self.request.finish()
                self.stopProducing()
        
    def read_piece(self):
        # probably file exsists on filesystem because have_piece()==True success check
        # now we can open it
        if not hasattr(self, 'fileObject') or self.fileObject.closed:
            self.fileObject = open(self.fileinfo.handle.save_path() + self.fileinfo.info.path, 'rb')
            self.fileObject.seek(self.offset)

        data = self.fileObject.read(self.piecelength - self.piece.start)
 
        if data:
            self.offset += len(data)
            self.request.write(data)
        del data

        if self.offset < self.lastoffset:
            # move to next piece
            self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, 0)
        elif self.request:
            self.request.unregisterProducer()
            self.request.finish()
            self.stopProducing()

    def stopProducing(self):
        if hasattr(self, 'fileObject') and not self.fileObject.closed:
            self.fileObject.close()
        super(StaticTorrentProducer, self).stopProducing()

    def resumeProducing(self):
        print("index", self.piece.piece)
        if self.fileinfo.handle.have_piece(self.piece.piece):
            self.cansend = False
            self.read_piece()
        else:
            self.cansend = True


class TorrentProducer(StaticTorrentProducer):
    pass


class Files_List_Update_Alert(object):
    _what = 'files_list_update_alert'
    _message = '{} files updated'
    def __init__(self, files):
        self.files = files
    def what(self):
        return self._what
    def message(self):
        return self._message.format(len(self.files))

class TorrentStream(static.File):
    isLeaf = True
    PAUSE = 0
    LOW = 1
    NORMAL = 4
    HIGHEST = 7
    def __init__(self, **options):
        self._alert_handlers = {}
        self._files_list = {}
        self.options = options
        self.options.setdefault('save_path', '/tmp/')

        print("libtorrent", libtorrent.version)
        self.session = session = libtorrent.session()
        #reactor.addSystemEventTrigger('before', 'shutdown', self.shutdown)
        reactor.callInThread(self._alert_queue_loop)

        session.set_alert_mask(
                libtorrent.alert.category_t.tracker_notification |
                libtorrent.alert.category_t.storage_notification |
                libtorrent.alert.category_t.progress_notification |
                libtorrent.alert.category_t.status_notification |
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
        session_settings.upload_rate_limit = 1024 * 1024 / 8
        session.set_settings(session_settings)

        session.add_dht_router("router.bittorrent.com", 6881)
        session.add_dht_router("router.utorrent.com", 6881)

        encryption_settings = libtorrent.pe_settings()
        encryption_settings.out_enc_policy = libtorrent.enc_policy(libtorrent.enc_policy.forced)
        encryption_settings.in_enc_policy = libtorrent.enc_policy(libtorrent.enc_policy.forced)
        encryption_settings.allowed_enc_level = libtorrent.enc_level.both
        encryption_settings.prefer_rc4 = True
        session.set_pe_settings(encryption_settings)

        #try:
        #    with open("session.state", 'rb') as fd:
        #        state = libtorrent.bdecode(fd.read())
        #except (IOError, EOFError, RuntimeError) as e:
        #    print("Unable to load session.state", e)
        #else:
        #    self.session.load_state(state)

        for file in glob.glob(self.options.get('save_path') + '/*.fastresume'):
            try:
                if os.path.exists(file):
                    with open(file, 'rb') as fd:
                        self.add_torrent(resume_data = fd.read())
            except (IOError, EOFError, RuntimeError) as e:
                print("Unable to load fastresume", e)

        def torrent_checked_alert(alert):
            #alert.handle.resume()
            alert.handle.prioritize_pieces(alert.handle.get_torrent_info().num_pieces() * [TorrentStream.PAUSE])

        def metadata_received_alert(alert):
            print('got {} files'.format(alert.handle.get_torrent_info().num_files()))
            for i in range(alert.handle.get_torrent_info().num_files()):
                info = alert.handle.get_torrent_info().file_at(i)
                self._files_list[info.path] = FileInfo(id=i, handle=alert.handle, info=info)
            self._handle_alert([Files_List_Update_Alert(self.list_files())])

        def torrent_added_alert(alert):
            if alert.handle.get_torrent_info():
                metadata_received_alert(alert)

        def tracker_announce_alert(alert):
            self._handle_alert([Files_List_Update_Alert(self.list_files())])

        def torrent_removed_alert(alert):
            info_hash = str(alert.handle.info_hash())
            for path, handle in self._files_list.items():
                if str(handle.handle.info_hash()) == info_hash:
                    del self._files_list[path]
            self._handle_alert([Files_List_Update_Alert(self.list_files())])

        def torrent_error_alert(alert):
            self.session.remove_torrent(alert.handle)

        #def files_list_update_alert(alert):
        #    try:
        #        with open("session.state", 'wb') as fd:
        #            fd.write(libtorrent.bencode(self.session.save_state()))
        #            #fd.flush()
        #            #os.fsync(.fileno())
        #    except (IOError, EOFError) as e:
        #        print("Unable to save session.state", e)

        #def cache_flushed_alert(alert):
        #    for handle in self.session.get_torrents():
        #        self.save_resume_data(handle)
        #    gc.collect()

        def torrent_finished_alert(alert):
            self.save_resume_data(alert.handle)

        def save_resume_data_alert(alert):
            print("save_resume_data_alert", alert.handle.get_torrent_info().name())
            try:
                with open(alert.handle.save_path() + "/" + alert.handle.get_torrent_info().name() + ".fastresume", 'wb') as fd:
                    fd.write(libtorrent.bencode(alert.resume_data))
            except (IOError, EOFError) as e:
                print("Unable to save fastresume", e)

        self.add_alert_handler('torrent_added_alert', torrent_added_alert)
        self.add_alert_handler('metadata_received_alert', metadata_received_alert)
        self.add_alert_handler('torrent_checked_alert', torrent_checked_alert)
        self.add_alert_handler('torrent_removed_alert', torrent_removed_alert)
        self.add_alert_handler('torrent_error_alert', torrent_error_alert)
        #self.add_alert_handler('files_list_update_alert', files_list_update_alert)
        #self.add_alert_handler('cache_flushed_alert', cache_flushed_alert)
        self.add_alert_handler('torrent_finished_alert', torrent_finished_alert)
        self.add_alert_handler('save_resume_data_alert', save_resume_data_alert)
        self.add_alert_handler('tracker_announce_alert', tracker_announce_alert)

    def _alert_queue_loop(self):
        print("_alert_queue_loop")
        while reactor.running:
            if self.session.wait_for_alert(5000):
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

    def save_resume_data(self, handle):
        if handle.is_valid() and handle.has_metadata() and handle.need_save_resume_data():
            # flush_disk_cache
            # save_info_dict
            # only_if_modified
            handle.save_resume_data(libtorrent.save_resume_flags_t.flush_disk_cache<<1 | libtorrent.save_resume_flags_t.flush_disk_cache<<2)
 
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

    def add_torrent(self, url = None, resume_data = None):
        add_torrent_params = {}
        if resume_data:
            add_torrent_params['resume_data'] = resume_data
            add_torrent_params['flag_override_resume_data'] = True
        if url:
            add_torrent_params['url'] = url
        if len(add_torrent_params):
            add_torrent_params['save_path'] = self.options.get('save_path')
            add_torrent_params['storage_mode'] = libtorrent.storage_mode_t.storage_mode_sparse
            add_torrent_params['auto_managed'] = False
            add_torrent_params['paused'] = False
            self.session.async_add_torrent(add_torrent_params)
            return True
        else:
            return False

    def remove_torrent(self, info_hash):
        try:
            handle = self.session.find_torrent(libtorrent.sha1_hash(info_hash.decode('hex')))
            if handle.is_valid():
                ti = handle.get_torrent_info()
                if ti:
                    fastresume = handle.save_path() + "/" + ti.name() + '.fastresume'
                    if os.path.exists(fastresume):
                        os.remove(fastresume)
                self.session.remove_torrent(handle, libtorrent.options_t.delete_files)
                return {'status': '{} removed'.format(info_hash)}
        except TypeError:
            return {'error': '{} incorrect hash'.format(info_hash)}
        return {'error': '{} not found'.format(info_hash)}

    def pause_torrent(self, info_hash):
        try:
            handle = self.session.find_torrent(libtorrent.sha1_hash(info_hash.decode('hex')))
            if handle.is_valid():
                if handle.status().paused:
                    handle.resume()
                    return {'status': '{} resumed'.format(info_hash)}
                else:
                    handle.pause()
                    return {'status': '{} paused'.format(info_hash)}
        except TypeError:
            return {'error': '{} incorrect hash'.format(info_hash)}
        return {'error': '{} not found'.format(info_hash)}

    def flush_torrent(self):
        try:
            for handle in self.session.get_torrents():
                if handle.is_valid():
                    handle.flush_cache()
            gc.collect()
            return {'status': 'flushed'}
        except TypeError:
            return {'error': 'incorrect hash'}
        return {'error': 'not found'}

    def list_files(self):
        directory = []
        for handle in self.session.get_torrents():
            if handle.is_valid():
                data = {}
                data['info_hash'] = str(handle.info_hash())
                data['files'] = []
                ti = handle.get_torrent_info()
                if ti:
                    data['title'] = ti.name()
                    for file in handle.get_torrent_info().files():
                        data['files'].append(file.path)
                    data['files'].sort()
                else:
                    data['title'] = str(handle.info_hash())
                directory.append(data)
        return sorted(directory, key=lambda data: data['title'])

    #def list_files(self):
    #    return sorted(self._files_list)

    def status(self):
        def space_break(string, length):
            return ' '.join(string[i:i+length] for i in xrange(0,len(string),length))
        status = {}
        sst = self.session.status()
        status['dht_nodes'] = sst.dht_nodes
        cst = self.session.get_cache_status()
        status['cache_size'] = cst.cache_size
        status['reads'] = cst.reads
        status['writes'] = cst.writes
        #status['write_cache_size'] = cst.write_cache_size
        status['read_cache_size'] = cst.read_cache_size
        for handle in self.session.get_torrents():
            info_hash = str(handle.info_hash())
            s = {}
            if handle.has_metadata():
                piece_map = ''
                for piece_index in range(handle.get_torrent_info().num_pieces()):
                    if handle.have_piece(piece_index):
                        piece_map += '*'
                    else:
                        piece_map += str(handle.piece_priority(piece_index))
                s['pieces'] = space_break(piece_map, 100)
                file_map = ''
                for file_index in range(handle.get_torrent_info().num_files()):
                    file_map += str(handle.file_priority(file_index))
                s['files'] = file_map
            s['has_metadata'] = handle.has_metadata()
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
            s['trackers'] = handle.trackers()
            s['upload_mode'] = st.upload_mode
            status[info_hash] = s
        return status

    @defer.inlineCallbacks
    def shutdown(self):
        outstanding_resume_data = 0
        for handle in self.session.get_torrents():
            print("check", handle.get_torrent_info().name(), handle.is_valid(), handle.has_metadata(), handle.need_save_resume_data())
            if not handle.is_valid():
                continue
            if not handle.has_metadata():
                continue
            if not handle.need_save_resume_data():
                continue
            handle.save_resume_data()
            outstanding_resume_data += 1

        if outstanding_resume_data:
            print("outstanding_resume_data", outstanding_resume_data)
            lock = defer.DeferredLock()
            def save_resume_data_alert(alert):
                print("name", alert.handle.get_torrent_info().name())
                outstanding_resume_data -= 1
                if not outstanding_resume_data:
                    lock.release()

            self.add_alert_handler('save_resume_data_alert', save_resume_data_alert)
            yield lock.acquire()
        print("torrentstream shutdown")

    @staticmethod
    def getTypeAndEncoding(url):
        return static.getTypeAndEncoding(url,
                                        static.File.contentTypes,
                                        static.File.contentEncodings,
                                              "text/html")

    def getFileSize(self):
        return self.fileForReading.info.size

    def makeProducer(self, request):
        byteRange = request.getHeader(b'range')
        if byteRange is None:
            self._setContentHeaders(request)
            request.setResponseCode(http.OK)
            return 0, None
        try:
            parsedRanges = self._parseRangeHeader(byteRange)
        except ValueError:
            self._setContentHeaders(request)
            request.setResponseCode(http.OK)
            return 0, None

        if len(parsedRanges) == 1:
            offset, size = self._doSingleRangeRequest(
                request, parsedRanges[0])
            self._setContentHeaders(request, size)
            return offset, size
        else:
            rangeInfo = self._doMultipleRangeRequest(request, parsedRanges)
            return rangeInfo, None

    def render_GET(self, request):
        url = request.args.get('url',[None])[0]
        ret = None

        def help():
            prepath = '{}:{}/{}'.format(request.host.host, request.host.port, '/'.join(request.prepath))
            return {'example': [ '{p}/add?url=http%3A%2F%2Fnewstudio.tv%2Fdownload.php%3Fid%3D17544'.format(p=prepath),
                                '{p}/get?url=file.avi'.format(p=prepath),
                                '{p}/rm?url=3bebb88255c4e3a2080b514a47a41fe75cbd8a40'.format(p=prepath),
                                '{p}/info'.format(p=prepath),
                                '{p}/ls'.format(p=prepath)
                              ]}

        if len(request.postpath) == 0:
            ret = help()
        elif request.postpath[0] == 'add' and url:
            self.add_torrent(url)
            ret = {'status': '{} added'.format(url)}
        elif request.postpath[0] == 'info':
            ret = self.status()
        elif request.postpath[0] == 'ls':
            ret = self.list_files()
        elif request.postpath[0] == 'get' and url:
            if url not in self._files_list:
                ret = {'error': '{} not found'.format(url)}
            else:
                self.type, self.encoding = self.getTypeAndEncoding(url)

                request.setHeader('accept-ranges', 'bytes')

                self.fileForReading = self._files_list[url]
                offset, size = self.makeProducer(request)

                if request.method == 'HEAD':
                    return ''

                producer = TorrentProducer(self, request, self.fileForReading, offset, size)
                producer.start()
                return server.NOT_DONE_YET
        elif request.postpath[0] == 'rm' and url:
            ret = self.remove_torrent(url)
        elif request.postpath[0] == 'pause' and url:
            ret = self.pause_torrent(url)
        elif request.postpath[0] == 'flush':
            ret = self.flush_torrent()
        else:
            ret = help()
        return json.dumps(ret)+'\n'

    render_HEAD = render_GET


def main():
    root = Resource()
    torrentstream = TorrentStream()
    root.putChild("bt", torrentstream)
    site = server.Site(root)
    reactor.listenTCP(8882, site)

if __name__ == '__main__':
    reactor.callWhenRunning(main)
    reactor.run()
