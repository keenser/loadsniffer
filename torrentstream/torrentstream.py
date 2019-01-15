#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
"""
torrent to http proxy module
"""

import mimetypes
import glob
import os
import asyncio
from collections import namedtuple
import logging
import binascii
import aiofiles
from aiohttp import web
import libtorrent

FileInfo = namedtuple('FileInfo', ('id', 'handle', 'info'))


class DynamicTorrentProducer:
    """read data using read_piece + read_piece_alert"""
    def __init__(self, stream, request, fileinfo, offset=0, size=None):
        self.log = logging.getLogger(self.__class__.__name__)
        self.stream = stream
        self.request = request
        self.fileinfo = fileinfo
        self.offset = offset
        self.size = size or fileinfo.info.size - offset
        self.lastoffset = self.offset + self.size - 1
        self.priority_window = None
        self.piece = None
        self.buffer = {}
        self.log.info("starting %s offset: %d size: %d", self.fileinfo.info.path, self.offset, self.size)

    def _read_piece_alert(self, alert):
        self.log.debug("read_piece_alert %d %d", alert.piece, alert.size)
        self.buffer[alert.piece] = alert.buffer
        self.request.resume()

    def _piece_finished_alert(self, alert):
        self.log.debug("piece_finished_alert")
        self._slide()
        self.request.resume()

    async def _read_piece(self):
        self.log.debug("read_piece %d %d %d", self.piece.piece, self.piece.start, self.piece.start + self.lastoffset - self.offset)
        buffer = self.buffer[self.piece.piece][self.piece.start:self.piece.start + self.lastoffset - self.offset]
        await self.request.write(buffer)
        await self.request.drain()
        self.offset += len(buffer)
        del self.buffer[self.piece.piece]

        if self.offset < self.lastoffset:
            # move to next piece
            self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, 0)
        else:
            raise asyncio.CancelledError

    async def resumeProducing(self):
        """continue torrent download iteration"""
        self.log.debug("index %d %s", self.piece.piece, self.buffer.keys())
        for window in range(self.piece.piece, min(self.lastpiece.piece + 1, self.piece.piece + len(self.prioritymask))):
            if not window in self.buffer and self.fileinfo.handle.have_piece(window):
                self.buffer[window] = None
                self.fileinfo.handle.read_piece(window)
        if self.piece.piece in self.buffer and self.buffer[self.piece.piece]:
            await self._read_piece()

    async def stopProducing(self):
        """stop torrent download"""
        self.log.info("stopProducing %s size: %d", self.fileinfo.info.path, self.size)
        self.stream.remove_alert_handler('read_piece_alert', self._read_piece_alert, self.fileinfo.handle)
        self.stream.remove_alert_handler('piece_finished_alert', self._piece_finished_alert, self.fileinfo.handle)

    async def start(self):
        """start downloading torrent file"""
        self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, 0)
        self.lastpiece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.lastoffset, 0)
        self.piecelength = self.fileinfo.handle.get_torrent_info().piece_length()
        self.log.debug("start %d %d %d %d", self.size, self.piece.piece, self.lastpiece.piece, self.piecelength)

        if self.piece.piece > self.lastpiece.piece:
            raise asyncio.CancelledError

        self.stream.add_alert_handler('read_piece_alert', self._read_piece_alert, self.fileinfo.handle)
        self.stream.add_alert_handler('piece_finished_alert', self._piece_finished_alert, self.fileinfo.handle)

        # priority window size 4Mb * 8
        priorityblock = int((4 * 1024 * 1024) / self.piecelength)
        # piece_length more than 4Mb ?
        if priorityblock < 1:
            priorityblock = 1
        elif priorityblock > 8:
            priorityblock = 8
        self.prioritymask = [i for i in [TorrentStream.HIGHEST, TorrentStream.HIGHEST, 6, 5, 4, 3, 2, 1] for _ in range(priorityblock)]
        self.log.debug("prioritymask %s", self.prioritymask)

        self.fileinfo.handle.resume()
        self._slide(self.piece.piece)

    def _slide(self, offset=None):
        if offset is not None:
            self.priority_window = offset
        window = self.priority_window
        data = []
        for priority in self.prioritymask:
            while True:
                if window > self.lastpiece.piece:
                    self.log.debug('slide %s', data)
                    return
                if self.fileinfo.handle.have_piece(window):
                    if window == self.priority_window:
                        self.priority_window += 1
                    window += 1
                else:
                    data.append(window)
                    self.fileinfo.handle.set_piece_deadline(window, 3000)
                    self.fileinfo.handle.piece_priority(window, priority)
                    window += 1
                    break
        self.log.debug('slide %s', data)


class StaticTorrentProducer(DynamicTorrentProducer):
    """speedup reading pieces using direct access to file on filesystem"""
    async def _read_piece_1(self):
        """open file every iteration"""
        async with aiofiles.open(os.path.join(self.fileinfo.handle.save_path(), self.fileinfo.info.path), mode='rb') as fileObject:
            await fileObject.seek(self.offset)
            data = await fileObject.read(self.piecelength - self.piece.start)

            if data:
                self.offset += len(data)
                await self.request.write(data)
                await self.request.drain()
            del data

            if self.offset < self.lastoffset:
                self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, 0)
            else:
                raise asyncio.CancelledError

    async def _read_piece(self):
        """open file ones"""
        # probably file exsists on filesystem because have_piece()==True success check
        # now we can open it
        if not hasattr(self, 'fileObject') or self.fileObject.closed:
            self.fileObject = await aiofiles.open(os.path.join(self.fileinfo.handle.save_path(), self.fileinfo.info.path), mode='rb')
            await self.fileObject.seek(self.offset)

        if self.piece.piece < self.lastpiece.piece:
            readlen = self.piecelength - self.piece.start
        else:
            readlen = self.lastpiece.start - self.piece.start + 1
        data = await self.fileObject.read(readlen)

        if data:
            self.offset += len(data)
            await self.request.write(data)
            await self.request.drain()

        if self.offset < self.lastoffset:
            # move to next piece
            self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, 0)
        else:
            raise asyncio.CancelledError

    async def stopProducing(self):
        """stop torrent download"""
        if hasattr(self, 'fileObject') and not self.fileObject.closed:
            await self.fileObject.close()
        await super().stopProducing()

    async def resumeProducing(self):
        """continue torrent download iteration"""
        self.log.debug("index %d %d", self.size, self.piece.piece)
        if self.fileinfo.handle.have_piece(self.piece.piece):
            await self._read_piece()


class TorrentProducer(StaticTorrentProducer):
    """inherit actual producer method"""
    pass


class FilesListUpdateAlert:
    """custom libtorrent alert called from TorrentStream"""
    _what = 'files_list_update_alert'
    _message = '{} files updated'

    def __init__(self, files):
        self.files = files

    def what(self):
        return self._what

    def message(self):
        return self._message.format(len(self.files))


class TorrentStream:
    """Main class"""
    PAUSE = 0
    LOW = 1
    NORMAL = 4
    HIGHEST = 7

    def __init__(self, **options):
        self.log = logging.getLogger(self.__class__.__name__)
        self._alert_handlers = {}
        self._files_list = {}
        self.options = options
        self.options.setdefault('save_path', '/tmp/')
        self.loop = options.get('loop', asyncio.get_event_loop())
        self.queue_event = asyncio.Event()

        self.http = web.Application()
        self.http.router.add_get('/{action:.*}', self.render_GET)
        self.http.on_shutdown.append(self.shutdown)

        self.log.info("libtorrent %s", libtorrent.version)
        self.session = session = libtorrent.session()
        self.queue_loop = self.loop.run_in_executor(None, self._alert_queue_loop)

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
        session_settings.upload_rate_limit = int(1024 * 1024 / 8)
        session.set_settings(session_settings)

        session.add_dht_router("router.bittorrent.com", 6881)
        session.add_dht_router("router.utorrent.com", 6881)

        encryption_settings = libtorrent.pe_settings()
        encryption_settings.out_enc_policy = libtorrent.enc_policy(libtorrent.enc_policy.forced)
        encryption_settings.in_enc_policy = libtorrent.enc_policy(libtorrent.enc_policy.forced)
        encryption_settings.allowed_enc_level = libtorrent.enc_level.both
        encryption_settings.prefer_rc4 = True
        session.set_pe_settings(encryption_settings)

        for file in glob.glob(self.options.get('save_path') + '/*.fastresume'):
            try:
                if os.path.exists(file):
                    with open(file, 'rb') as fd:
                        self.add_torrent(resume_data=fd.read())
            except (IOError, EOFError, RuntimeError) as exception:
                self.log.error("Unable to load fastresume %s", exception)

        def torrent_checked_alert(alert):
            alert.handle.prioritize_pieces(alert.handle.get_torrent_info().num_pieces() * [TorrentStream.PAUSE])

        def metadata_received_alert(alert):
            self.log.info('got %d files', alert.handle.get_torrent_info().num_files())
            for i in range(alert.handle.get_torrent_info().num_files()):
                info = alert.handle.get_torrent_info().file_at(i)
                self._files_list[info.path] = FileInfo(id=i, handle=alert.handle, info=info)
            self._handle_alert([FilesListUpdateAlert(self.list_files())])

        def torrent_added_alert(alert):
            if alert.handle.get_torrent_info():
                metadata_received_alert(alert)

        def tracker_announce_alert(alert):
            self._handle_alert([FilesListUpdateAlert(self.list_files())])

        def torrent_removed_alert(alert):
            info_hash = str(alert.handle.info_hash())
            for path, handle in dict(self._files_list).items():
                if str(handle.handle.info_hash()) == info_hash:
                    del self._files_list[path]
                    self._handle_alert([FilesListUpdateAlert(self.list_files())])

        def torrent_error_alert(alert):
            self.session.remove_torrent(alert.handle)

        def torrent_finished_alert(alert):
            self.save_resume_data(alert.handle)

        def file_completed_alert(alert):
            self.save_resume_data(alert.handle)

        def save_resume_data_alert(alert):
            self.log.info("save_resume_data_alert %s", alert.handle.get_torrent_info().name())
            try:
                with open(alert.handle.save_path() + "/" + alert.handle.get_torrent_info().name() + ".fastresume", 'wb') as fd:
                    fd.write(libtorrent.bencode(alert.resume_data))
            except (IOError, EOFError) as e:
                self.log.error("Unable to save fastresume %s", e)

        self.add_alert_handler('torrent_added_alert', torrent_added_alert)
        self.add_alert_handler('metadata_received_alert', metadata_received_alert)
        self.add_alert_handler('torrent_checked_alert', torrent_checked_alert)
        self.add_alert_handler('torrent_removed_alert', torrent_removed_alert)
        self.add_alert_handler('torrent_error_alert', torrent_error_alert)
        self.add_alert_handler('torrent_finished_alert', torrent_finished_alert)
        self.add_alert_handler('file_completed_alert', file_completed_alert)
        self.add_alert_handler('save_resume_data_alert', save_resume_data_alert)
        self.add_alert_handler('tracker_announce_alert', tracker_announce_alert)

    def _alert_queue_loop(self):
        self.log.debug("enter to _alert_queue_loop")
        try:
            while not self.queue_event.is_set():
                if self.session.wait_for_alert(1000):
                    self.loop.call_soon_threadsafe(self._handle_alert, self.session.pop_alerts())
        except asyncio.CancelledError:
            return

    def _handle_alert(self, alerts):
        for alert in alerts:
            if alert.what() != 'block_finished_alert' and alert.what() != 'block_downloading_alert':
                try:
                    self.log.debug('%s: %s', alert.what(), alert.message())
                except:
                    self.log.debug('%s', alert.what())
            if alert.what() in self._alert_handlers:
                for handler in self._alert_handlers[alert.what()]:
                    if asyncio.iscoroutinefunction(handler):
                        self.loop.create_task(handler(alert))
                    else:
                        handler(alert)
            if hasattr(alert, 'handle'):
                what = str(alert.handle.info_hash()) + ':' + alert.what()
                for handler in self._alert_handlers.get(what, []):
                    if asyncio.iscoroutinefunction(handler):
                        self.loop.create_task(handler(alert))
                    else:
                        handler(alert)

    def save_resume_data(self, handle):
        if handle.is_valid() and handle.has_metadata() and handle.need_save_resume_data():
            # flush_disk_cache
            # save_info_dict
            # only_if_modified
            handle.save_resume_data(libtorrent.save_resume_flags_t.flush_disk_cache<<1 | libtorrent.save_resume_flags_t.flush_disk_cache<<2)

    def add_alert_handler(self, alert, handler, handle=None):
        """register new callback on specific alert and optional on specific torrent handle"""
        if handle:
            alert = str(handle.info_hash()) + ':' + alert
        if handler not in self._alert_handlers.setdefault(alert, []):
            self._alert_handlers[alert].append(handler)

    def remove_alert_handler(self, alert, handler, handle=None):
        """remove callback from alert"""
        if handle:
            alert = str(handle.info_hash()) + ':' + alert
        if alert in self._alert_handlers and handler in self._alert_handlers[alert]:
            self._alert_handlers[alert].remove(handler)
            if not self._alert_handlers[alert]:
                self._alert_handlers.pop(alert)
        else:
            self.log.warning("remove alert %s handler %s not in handlers", alert, handler)

    def add_torrent(self, url=None, resume_data=None):
        """add torrent or magnet available over url or/and resume_data file"""
        add_torrent_params = {}
        if resume_data:
            add_torrent_params['resume_data'] = resume_data
            add_torrent_params['flag_override_resume_data'] = True
        if url:
            add_torrent_params['url'] = url
        if add_torrent_params:
            add_torrent_params['save_path'] = self.options.get('save_path')
            add_torrent_params['storage_mode'] = libtorrent.storage_mode_t.storage_mode_sparse
            add_torrent_params['auto_managed'] = False
            add_torrent_params['paused'] = False
            self.session.async_add_torrent(add_torrent_params)
            return True
        return False

    def remove_torrent(self, info_hash):
        """remove torrent from list by info_hash"""
        try:
            handle = self.session.find_torrent(libtorrent.sha1_hash(binascii.unhexlify(info_hash)))
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
        """pause/resume torrent"""
        try:
            handle = self.session.find_torrent(libtorrent.sha1_hash(binascii.unhexlify(info_hash)))
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
        """flush cache on all torrents"""
        try:
            for handle in self.session.get_torrents():
                if handle.is_valid():
                    handle.flush_cache()
            return {'status': 'flushed'}
        except TypeError:
            return {'error': 'incorrect hash'}

    def list_files(self):
        """list available files in torrents"""
        directory = []
        for handle in self.session.get_torrents():
            if handle.is_valid():
                data = {
                    'info_hash': str(handle.info_hash()),
                    'files': []
                }
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

    def status(self):
        """dump torrent status"""
        def space_break(string, length):
            return ' '.join(string[i:i+length] for i in range(0, len(string), length))
        status = {}
        sst = self.session.status()
        status['version'] = libtorrent.version
        status['dht_nodes'] = sst.dht_nodes
        cst = self.session.get_cache_status()
        status['cache_size'] = cst.cache_size
        status['reads'] = cst.reads
        status['writes'] = cst.writes
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
                s['name'] = handle.get_torrent_info().name()
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

    async def shutdown(self, app):
        self.queue_event.set()
        await asyncio.wait([self.queue_loop])
        self.log.info("shutdown done")

    async def render_GET(self, request):
        url = request.query.get('url', None)
        action = request.match_info.get('action')
        ret = None

        def help():
            def rstrip(pattern, string):
                return string[:-len(pattern)] if string.endswith(pattern) and pattern else string

            prepath = '{}{}'.format(request.host, rstrip(action, request.path))
            return {'example': [
                '{p}add?url=http%3A%2F%2Fnewstudio.tv%2Fdownload.php%3Fid%3D17544'.format(p=prepath),
                '{p}rm?url=3bebb88255c4e3a2080b514a47a41fe75cbd8a40'.format(p=prepath),
                '{p}info'.format(p=prepath),
                '{p}ls'.format(p=prepath),
                '{p}file.avi'.format(p=prepath),
                ]}

        if action == 'add' and url:
            self.add_torrent(url)
            ret = {'status': '{} added'.format(url)}
        elif action == 'info':
            ret = self.status()
        elif action == 'ls':
            ret = self.list_files()
        elif action == 'rm' and url:
            ret = self.remove_torrent(url)
        elif action == 'pause' and url:
            ret = self.pause_torrent(url)
        elif action == 'flush':
            ret = self.flush_torrent()
        else:
            if action not in self._files_list:
                ret = help()
            else:
                fileForReading = self._files_list[action]
                mimetype = mimetypes.guess_type(action, strict=False)[0] or 'application/octet-stream'
                filesize = fileForReading.info.size

                ranges = request.http_range
                offset = ranges.start or 0
                stop = ranges.stop or filesize
                rangestr = 'bytes {}-{}/{}'.format(offset, stop - 1, filesize)
                size = stop - offset
                status = 200 if ranges.start is None and ranges.stop is None else 206

                resume = asyncio.Event()

                class StreamResponse(web.StreamResponse):
                    async def write(self, data):
                        self.resume()
                        await super().write(data)

                    @staticmethod
                    def resume():
                        if not resume.is_set():
                            resume.set()

                resp = StreamResponse(status=status,
                                      headers={
                                          'accept-ranges': 'bytes',
                                          'Content-Type': mimetype,
                                          'content-length': str(size),
                                          'content-range': rangestr,
                                          'Content-Disposition': 'inline; filename="{}"'.format(os.path.basename(action))}
                                     )

                if request.method == 'HEAD':
                    return resp

                await resp.prepare(request)
                producer = TorrentProducer(self, resp, fileForReading, offset, size)
                try:
                    await producer.start()
                    while True:
                        resume.clear()
                        await producer.resumeProducing()
                        try:
                            await asyncio.wait_for(resume.wait(), 5)
                        except asyncio.TimeoutError:
                            pass
                except asyncio.CancelledError:
                    """raise for stopProducing"""
                    raise
                finally:
                    await producer.stopProducing()

                return resp

        return web.json_response(ret)

