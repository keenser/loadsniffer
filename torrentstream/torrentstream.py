#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
"""
torrent to http proxy module
"""
from __future__ import annotations
import mimetypes
import glob
import os
import asyncio
import logging
import binascii
from typing import Dict, List, NamedTuple, Optional, cast
import aiofiles
from aiohttp import web, http_writer
from pydantic import BaseModel
import libtorrent
import socket
import json

class FileInfo(NamedTuple):
    id: int
    handle: libtorrent.torrent_handle
    info: libtorrent.file_entry

class Piece(NamedTuple):
    length: int
    piece: int
    start: int

class DynamicTorrentProducer:
    """read data using read_piece + read_piece_alert"""
    def __init__(self, stream:TorrentStream, fileinfo:FileInfo, offset=0, size:Optional[int]=None):
        self.log = logging.getLogger('{}.{}'.format('torrent', self.__class__.__name__))
        self.stream = stream
        self.fileinfo = fileinfo
        self.offset = offset
        self.size = size or fileinfo.info.size - offset
        self.lastoffset = self.offset + self.size - 1
        self.priority_window:int
        self.piece:Piece
        self.buffer = {}
        self._piece_ready = asyncio.Event()
        self.log.info("starting %s offset: %d size: %d", self.fileinfo.info.path, self.offset, self.size)

    def _read_piece_alert(self, alert):
        self.log.debug("read_piece_alert %d %d", alert.piece, alert.size)
        self.buffer[alert.piece] = alert.buffer
        self._piece_ready.set()

    def _piece_finished_alert(self, alert):
        self._slide()
        self._piece_ready.set()

    async def _piece_available(self) -> bool:
        """prefetch pieces in the priority window, report if the current one is ready"""
        for window in range(self.piece.piece, min(self.lastpiece.piece + 1, self.piece.piece + len(self.prioritymask))):
            if not window in self.buffer and self.fileinfo.handle.have_piece(window):
                self.buffer[window] = None
                self.fileinfo.handle.read_piece(window)
        return bool(self.buffer.get(self.piece.piece))

    async def _read_piece(self) -> bytes:
        self.log.debug("read_piece %d %d %d", self.piece.piece, self.piece.start, self.piece.start + self.lastoffset - self.offset + 1)
        buffer = self.buffer[self.piece.piece][self.piece.start:self.piece.start + self.lastoffset - self.offset + 1]
        self.offset += len(buffer)
        del self.buffer[self.piece.piece]

        if self.offset < self.lastoffset:
            # move to next piece
            self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, 0)

        return bytes(buffer)

    async def _cleanup(self):
        """hook for subclasses needing extra teardown"""

    async def __aiter__(self) -> AsyncGenerator[bytes, None]:
        """start downloading torrent file and stream it"""
        self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, 0)
        self.lastpiece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.lastoffset, 0)
        self.piecelength = self.fileinfo.handle.get_torrent_info().piece_length()
        self.log.debug("start %d %d %d %d", self.size, self.piece.piece, self.lastpiece.piece, self.piecelength)

        if self.piece.piece > self.lastpiece.piece:
            return

        self.stream.add_alert_handler('read_piece', self._read_piece_alert, self.fileinfo.handle)
        self.stream.add_alert_handler('piece_finished', self._piece_finished_alert, self.fileinfo.handle)

        try:
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

            while self.offset <= self.lastoffset:
                while not await self._piece_available():
                    self._piece_ready.clear()
                    await self._piece_ready.wait()

                chunk = await self._read_piece()
                if chunk:
                    yield chunk
        finally:
            await self._cleanup()
            self.stream.remove_alert_handler('read_piece', self._read_piece_alert, self.fileinfo.handle)
            self.stream.remove_alert_handler('piece_finished', self._piece_finished_alert, self.fileinfo.handle)

    def _slide(self, offset:Optional[int]=None):
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
    def __init__(self, stream:TorrentStream, fileinfo:FileInfo, offset=0, size:Optional[int]=None):
        super().__init__(stream=stream, fileinfo=fileinfo, offset=offset, size=size)
        self.fileObject = None

    async def _piece_available(self) -> bool:
        return self.fileinfo.handle.have_piece(self.piece.piece)

    async def _read_piece(self) -> bytes:
        """open file ones"""
        # probably file exsists on filesystem because have_piece()==True success check
        # now we can open it
        if self.fileObject is None or self.fileObject.closed:
            self.fileObject = await aiofiles.open(os.path.join(self.fileinfo.handle.save_path(), self.fileinfo.info.path), mode='rb')
            await self.fileObject.seek(self.offset)

        if self.piece.piece < self.lastpiece.piece:
            readlen = self.piecelength - self.piece.start
        else:
            readlen = self.lastpiece.start - self.piece.start + 1
        data = await self.fileObject.read(readlen)

        if data:
            self.offset += len(data)

        if self.offset < self.lastoffset:
            # move to next piece
            self.piece = self.fileinfo.handle.get_torrent_info().map_file(self.fileinfo.id, self.offset, 0)

        return data

    async def _cleanup(self):
        """stop torrent download"""
        if self.fileObject is not None and not self.fileObject.closed:
            await self.fileObject.close()


class TorrentProducer(StaticTorrentProducer):
    """inherit actual producer method"""
    pass


class TorrentFileEntry(BaseModel):
    path: str
    id: int
    progress: float


class TorrentEntry(BaseModel):
    info_hash: str
    title: str
    progress: float
    files: List[TorrentFileEntry] = []


class TorrentStatus(BaseModel):
    name: Optional[str] = None
    pieces: Optional[str] = None
    paused: bool
    # str(), not the live libtorrent enum/error_code - those aren't JSON-serializable.
    state: str
    error: str
    progress: str
    download_rate: int
    upload_rate: int
    num_seeds: int
    num_peers: int


class SessionStatus(BaseModel):
    version: str
    torrents: Dict[str, TorrentStatus] = {}


class TorrentStreamer1:
    """Ленивый стриминг через read_piece + alert, без лишнего буфера"""

    def __init__(self, stream: 'TorrentStream', fileinfo: FileInfo,
                 offset: int = 0, size: Optional[int] = None):
        self.log = logging.getLogger('torrent.streamer')
        self.stream = stream
        self.fileinfo = fileinfo
        self.offset = offset
        self.size = size or fileinfo.info.size - offset
        self.lastoffset = self.offset + self.size - 1

        self.piecelength = None
        self.current_piece = None
        self.last_piece = None

        self._data_future: Optional[asyncio.Future] = None   # ← вместо buffer

    async def __aiter__(self) -> AsyncGenerator[bytes, None]:
        ti = self.fileinfo.handle.get_torrent_info()
        self.piecelength = ti.piece_length()
        self.current_piece = ti.map_file(self.fileinfo.id, self.offset, 0)
        self.last_piece = ti.map_file(self.fileinfo.id, self.lastoffset, 0)

        self._set_low_priority_range()

        self.fileinfo.handle.resume()

        self.stream.add_alert_handler('read_piece', self._on_read_piece, self.fileinfo.handle)

        try:
            while self.offset <= self.lastoffset:
                piece_idx = self.current_piece.piece
                piece_start = self.current_piece.start

                # Запрашиваем кусок
                self._data_future = asyncio.Future()
                self.fileinfo.handle.read_piece(piece_idx)          # ← запрос
                self.fileinfo.handle.set_piece_deadline(piece_idx, 6000)
                self.fileinfo.handle.piece_priority(piece_idx, TorrentStream.HIGHEST)

                # Ждём данные из алерта
                try:
                    data = await asyncio.wait_for(self._data_future, timeout=15.0)
                except asyncio.TimeoutError:
                    self.log.warning("Timeout reading piece %d", piece_idx)
                    continue
                except asyncio.CancelledError:
                    raise

                # Вырезаем нужный диапазон из piece
                chunk_start = piece_start
                chunk_end = min(len(data), chunk_start + (self.lastoffset - self.offset + 1))
                chunk = data[chunk_start:chunk_end]

                if chunk:
                    yield chunk
                    self.offset += len(chunk)

                # Переходим дальше
                if self.offset <= self.lastoffset:
                    self.current_piece = ti.map_file(self.fileinfo.id, self.offset, 0)

        finally:
            self.stream.remove_alert_handler('read_piece', self._on_read_piece, self.fileinfo.handle)
            if self._data_future and not self._data_future.done():
                self._data_future.cancel()

    def _set_low_priority_range(self):
        """Устанавливаем LOWEST приоритет всем pieces в запрашиваемом диапазоне"""
        start_idx = self.current_piece.piece
        end_idx = self.last_piece.piece

        self.log.debug("Setting LOW priority for pieces %d to %d", start_idx, end_idx)

        for i in range(start_idx, end_idx + 1):
            self.fileinfo.handle.piece_priority(i, TorrentStream.LOW)   # или PAUSE, если хочешь ещё ниже

    def _on_read_piece(self, alert):
        """Простой обработчик — кладём данные в future"""
        if self._data_future and not self._data_future.done():
            self._data_future.set_result(bytes(alert.buffer))   # копируем
        # Игнорируем алерты для других pieces


class TorrentStreamer:
    """Современный async generator для стриминга торрент-файлов"""

    def __init__(self, stream: 'TorrentStream', fileinfo: FileInfo,
                 offset: int = 0, size: Optional[int] = None):
        self.log = logging.getLogger('torrent.streamer')
        self.stream = stream
        self.fileinfo = fileinfo
        self.offset = offset
        self.size = size or fileinfo.info.size - offset
        self.lastoffset = self.offset + self.size - 1

        self.fileObject = None
        self.piecelength = None
        self.current_piece = None
        self.last_piece = None

    async def __aiter__(self) -> AsyncGenerator[bytes, None]:
        """Async generator — основной поток данных"""
        try:
            ti = self.fileinfo.handle.get_torrent_info()
            self.piecelength = ti.piece_length()
            self.current_piece = ti.map_file(self.fileinfo.id, self.offset, 0)
            self.last_piece = ti.map_file(self.fileinfo.id, self.lastoffset, 0)

            self.fileinfo.handle.resume()
            self._set_priorities()

            while self.offset <= self.lastoffset:
                # Определяем, сколько читать
                if self.current_piece.piece < self.last_piece.piece:
                    readlen = self.piecelength - self.current_piece.start
                else:
                    readlen = self.lastoffset - self.offset + 1

                # Ждём, пока кусок будет готов (с таймаутом)
                if not self.fileinfo.handle.have_piece(self.current_piece.piece):
                    await self._wait_for_piece(self.current_piece.piece)
                    continue

                if self.fileObject is None:
                    path = os.path.join(self.fileinfo.handle.save_path(), self.fileinfo.info.path)
                    self.fileObject = await aiofiles.open(path, 'rb')
                    await self.fileObject.seek(self.offset)

                data = await self.fileObject.read(readlen)
                if not data:
                    break

                yield data

                self.offset += len(data)

                # Переходим к следующему piece
                if self.offset <= self.lastoffset:
                    self.current_piece = ti.map_file(self.fileinfo.id, self.offset, 0)

                # Обновляем приоритеты каждые несколько pieces
                self._set_priorities()

        finally:
            if self.fileObject and not self.fileObject.closed:
                await self.fileObject.close()

    def _set_priorities(self):
        """Повышаем приоритет ближайших кусков"""
        start = self.current_piece.piece
        for i in range(12):  # окно приоритета ~12 pieces
            piece_idx = start + i
            if piece_idx <= self.last_piece.piece:
                self.fileinfo.handle.piece_priority(piece_idx, TorrentStream.HIGHEST)
                self.fileinfo.handle.set_piece_deadline(piece_idx, 5000)

    async def _wait_for_piece(self, piece_idx: int, timeout: float = 8.0):
        """Ждём появления piece с backpressure"""
        try:
            await asyncio.wait_for(
                self._wait_for_piece_alert(piece_idx),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            self.log.warning("Timeout waiting for piece %d", piece_idx)
            # Всё равно пробуем читать — libtorrent сам скачает

    async def _wait_for_piece_alert(self, piece_idx: int):
        """Ожидание алерта piece_finished"""
        event = asyncio.Event()

        def on_piece_finished(alert):
            if alert.piece_index == piece_idx:
                event.set()

        handler_id = f"piece_{piece_idx}"
        self.stream.add_alert_handler('piece_finished', on_piece_finished, self.fileinfo.handle)

        try:
            await event.wait()
        finally:
            self.stream.remove_alert_handler('piece_finished', on_piece_finished, self.fileinfo.handle)


class FilesListUpdateAlert:
    """custom libtorrent alert called from TorrentStream"""
    _what = 'files_list_update_alert'
    _message = '{} files updated'

    def __init__(self, files: List[TorrentEntry]):
        self.files = files

    def what(self):
        return self._what

    def message(self):
        return self._message.format(len(self.files))


class ProgressUpdateAlert:
    """custom libtorrent alert called from TorrentStream"""
    _what = 'progress_update_alert'
    _message = '{} updated'

    def __init__(self, progress):
        self.progress = progress

    def what(self):
        return self._what

    def message(self):
        return self._message.format(len(self.progress))

class TorrentStream:
    """Main class"""
    PAUSE = 0
    LOW = 1
    NORMAL = 4
    HIGHEST = 7

    def __init__(self, loop=None, **options:str):
        self.log = logging.getLogger('{}.{}'.format('torrent', self.__class__.__name__))
        self._alert_handlers = {}
        self._files_list = {}
        self.options = options
        self.options.setdefault('save_path', '/tmp/')
        self.loop = loop or asyncio.get_event_loop()

        self.http = web.Application()
        self.http.router.add_get('/{action:.*}', self.render_GET)
        self.http.on_shutdown.append(self.shutdown)

        self.log.info("libtorrent %s", libtorrent.version)
        self.session = session = libtorrent.session()

        session.start_dht()
        session.start_lsd()
        session.start_upnp()
        session.start_natpmp()

        session_settings = session.get_settings()
        session_settings['strict_end_game_mode'] = False
        session_settings['announce_to_all_tiers'] = True
        session_settings['announce_to_all_trackers'] = True
        session_settings['upload_rate_limit'] = int(1024 * 1024 / 8)
        session_settings['alert_mask'] = libtorrent.alert.category_t.tracker_notification | \
                                         libtorrent.alert.category_t.storage_notification | \
                                         libtorrent.alert.category_t.piece_progress_notification | \
                                         libtorrent.alert.category_t.file_progress_notification | \
                                         libtorrent.alert.category_t.status_notification | \
                                         libtorrent.alert.category_t.error_notification
        session.apply_settings(session_settings)

        session.add_dht_router("router.bittorrent.com", 6881)
        session.add_dht_router("router.utorrent.com", 6881)
        session.add_dht_router("dht.transmissionbt.com", 6881)
        session.add_dht_router("router.bitcomet.com", 6881)
        session.add_dht_router("dht.aelitis.com", 6881)

        encryption_settings = libtorrent.pe_settings()
        encryption_settings.out_enc_policy = libtorrent.enc_policy(libtorrent.enc_policy.forced)
        encryption_settings.in_enc_policy = libtorrent.enc_policy(libtorrent.enc_policy.forced)
        encryption_settings.allowed_enc_level = libtorrent.enc_level.both
        encryption_settings.prefer_rc4 = True
        session.set_pe_settings(encryption_settings)

        def torrent_checked_alert(alert):
            alert.handle.prioritize_pieces(alert.handle.get_torrent_info().num_pieces() * [TorrentStream.PAUSE])

        def metadata_received_alert(alert):
            self.log.info('got %d files', alert.handle.get_torrent_info().num_files())
            #for i in range(alert.handle.get_torrent_info().num_files()):
            #    info = alert.handle.get_torrent_info().file_at(i)
            #    self._files_list[info.path] = FileInfo(id=i, handle=alert.handle, info=info)
            self._handle_alert([FilesListUpdateAlert(self.list_files())])

        def torrent_added_alert(alert):
            if alert.handle.get_torrent_info():
                metadata_received_alert(alert)

        def tracker_announce_alert(alert):
            self._handle_alert([FilesListUpdateAlert(self.list_files())])

        def torrent_removed_alert(alert):
            #info_hash = str(alert.handle.info_hash())
            #for path, handle in dict(self._files_list).items():
            #    if str(handle.handle.info_hash()) == info_hash:
            #        del self._files_list[path]
            self._handle_alert([FilesListUpdateAlert(self.list_files())])

        def torrent_error_alert(alert):
            self.session.remove_torrent(alert.handle)

        def torrent_finished_alert(alert):
            self._save_resume_data(alert.handle)

        def file_completed_alert(alert):
            alert.handle.flush_cache()

        def cache_flushed_alert(alert):
            self._save_resume_data(alert.handle)

        async def save_resume_data(fn, fc):
            try:
                async with aiofiles.open(fn, mode='wb') as fd:
                    await fd.write(fc)
            except (IOError, EOFError) as e:
                self.log.error("Unable to save fastresume %s", e)

        def save_resume_data_alert(alert):
            self.log.info("save_resume_data_alert %s", alert.handle.get_torrent_info().name())
            fn = os.path.join(alert.handle.save_path(), alert.handle.get_torrent_info().name() + ".fastresume")
            fc = libtorrent.write_resume_data_buf(alert.params)
            self.loop.create_task(save_resume_data(fn, fc))

        def piece_finished_alert(alert):
            directory = {}
            ti = alert.handle.get_torrent_info()
            data = {}
            progress = alert.handle.file_progress()
            for num in range(ti.num_files()):
                file = ti.file_at(num)
                data[num] = progress[num] / file.size * 100.0

            data['progress'] = alert.handle.status().progress * 100.0
            directory[str(alert.handle.info_hash())] = data

            self._handle_alert([ProgressUpdateAlert(directory)])

        self.add_alert_handler('torrent_added', torrent_added_alert)
        self.add_alert_handler('metadata_received', metadata_received_alert)
        self.add_alert_handler('torrent_checked', torrent_checked_alert)
        self.add_alert_handler('torrent_deleted', torrent_removed_alert)
        self.add_alert_handler('torrent_error', torrent_error_alert)
        #self.add_alert_handler('torrent_finished', torrent_finished_alert)
        self.add_alert_handler('file_completed', file_completed_alert)
        self.add_alert_handler('cache_flushed', cache_flushed_alert)
        self.add_alert_handler('save_resume_data', save_resume_data_alert)
        # self.add_alert_handler('tracker_announce', tracker_announce_alert)
        self.add_alert_handler('piece_finished', piece_finished_alert)

        self.rfile, self.wfile = socket.socketpair()
        self.loop.add_reader(self.rfile, self._handle_alert)
        self.session.set_alert_fd(self.wfile.fileno())

        for file in glob.glob(self.options['save_path'] + '/*.fastresume'):
            try:
                if os.path.exists(file):
                    with open(file, 'rb') as fd:
                        self.add_torrent(resume_data=fd.read())
            except (IOError, EOFError, RuntimeError) as exception:
                self.log.error("Unable to load fastresume %s", exception)

    def _handle_alert(self, alerts=None):
        if not alerts:
            self.rfile.recv(1)
            alerts = self.session.pop_alerts()

        for alert in alerts:
            try:
                self.log.debug('%s: %s', alert.what(), alert.message())
            except:
                self.log.debug('%s', alert.what())

            if hasattr(alert, 'handle'):
                what = str(alert.handle.info_hash()) + ':' + alert.what()
                if what in self._alert_handlers:
                    for handler in self._alert_handlers[what]:
                        if asyncio.iscoroutinefunction(handler):
                            self.loop.create_task(handler(alert))
                        else:
                            handler(alert)
            if alert.what() in self._alert_handlers:
                for handler in self._alert_handlers[alert.what()]:
                    if asyncio.iscoroutinefunction(handler):
                        self.loop.create_task(handler(alert))
                    else:
                        handler(alert)

    def _save_resume_data(self, handle):
        if handle.is_valid() and handle.has_metadata() and handle.need_save_resume_data():
            handle.save_resume_data(libtorrent.save_resume_flags_t.save_info_dict | libtorrent.save_resume_flags_t.only_if_modified)

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
            self.log.warning("remove alert %s handler %s not in handlers:%s", alert, handler, self._alert_handlers)

    def add_torrent(self, url=None, resume_data=None):
        """add torrent or magnet available over url or/and resume_data file"""
        add_torrent_params = None
        if resume_data:
            add_torrent_params = libtorrent.read_resume_data(resume_data)
        elif url:
            add_torrent_params = libtorrent.add_torrent_params()
            add_torrent_params.url = url
            add_torrent_params.save_path = self.options['save_path']
            add_torrent_params.storage_mode = libtorrent.storage_mode_t.storage_mode_sparse
        if add_torrent_params:
            add_torrent_params.flags &= ~libtorrent.add_torrent_params_flags_t.flag_auto_managed
            add_torrent_params.flags &= ~libtorrent.add_torrent_params_flags_t.flag_paused
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

    def load_torrent(self, info_hash):
        """load torrent"""
        try:
            handle = self.session.find_torrent(libtorrent.sha1_hash(binascii.unhexlify(info_hash)))
            if handle.is_valid():
                handle.prioritize_pieces(handle.get_torrent_info().num_pieces() * [TorrentStream.LOW])
                return {'status': '{} loading'.format(info_hash)}
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

    def list_files(self) -> List[TorrentEntry]:
        """list available files in torrents"""
        directory = []
        files_list:Dict[str, FileInfo] = {}
        for handle in self.session.get_torrents():
            if handle.is_valid():
                files = []
                ti = handle.get_torrent_info()
                if ti:
                    # fix SIGSEGV
                    progress = handle.file_progress() if handle.status().progress else None
                    title = ti.name()
                    for num in range(ti.num_files()):
                        file = ti.file_at(num)
                        files.append(TorrentFileEntry(
                            path=file.path,
                            id=num,
                            progress=progress[num]/file.size * 100.0 if progress else 0,
                        ))
                        files_list[file.path] = FileInfo(id=num, handle=handle, info=file)
                    files.sort(key=lambda entry: entry.path)
                else:
                    title = str(handle.info_hash())
                directory.append(TorrentEntry(
                    info_hash=str(handle.info_hash()),
                    title=title,
                    progress=handle.status().progress * 100.0,
                    files=files,
                ))
        self._files_list = files_list
        return sorted(directory, key=lambda entry: entry.title)

    def recheck(self, info_hash):
        """recheck torrent"""
        try:
            handle = self.session.find_torrent(libtorrent.sha1_hash(binascii.unhexlify(info_hash)))
            handle.force_recheck()
            return {'status': '{} recheck'.format(info_hash)}
            #if handle.is_valid():
        except TypeError:
            return {'error': '{} incorrect hash'.format(info_hash)}
        return {'error': '{} not found'.format(info_hash)}

    def status(self) -> SessionStatus:
        """dump torrent status"""
        def space_break(string, length):
            string = [str(i) for i in string]
            return ' '.join(''.join(string[i:i+length]) for i in range(0, len(string), length))
        torrents: Dict[str, TorrentStatus] = {}

        for handle in self.session.get_torrents():
            info_hash = str(handle.info_hash())
            name = None
            pieces = None
            if handle.has_metadata():
                torrent_info = handle.get_torrent_info()
                piece_map = handle.get_piece_priorities()
                for piece_index in range(torrent_info.num_pieces()):
                    if handle.have_piece(piece_index):
                        piece_map[piece_index] = '*'

                pieces = space_break(piece_map, 100)
                name = torrent_info.name()
            st = handle.status()
            torrents[info_hash] = TorrentStatus(
                name=name,
                pieces=pieces,
                paused=st.paused,
                state=str(st.state),
                error=str(st.error),
                progress='{:.2%}'.format(st.progress),
                download_rate=st.download_rate,
                upload_rate=st.upload_rate,
                num_seeds=st.num_seeds,
                num_peers=st.num_peers,
            )
        return SessionStatus(version=libtorrent.version, torrents=torrents)

    async def shutdown(self, app):
        self.log.info("shutdown done")

    async def render_GET(self, request: web.Request):
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
            ret = self.status().model_dump(mode='json')
        elif action == 'ls':
            ret = [entry.model_dump(mode='json') for entry in self.list_files()]
        elif action == 'rm' and url:
            ret = self.remove_torrent(url)
        elif action == 'pause' and url:
            ret = self.pause_torrent(url)
        elif action == 'flush':
            ret = self.flush_torrent()
        elif action == 'recheck':
            ret = self.recheck(url)
        else:
            if action not in self._files_list:
                ret = help()
            else:
                fileinfo = self._files_list[action]
                mimetype = mimetypes.guess_type(action, strict=False)[0] or 'application/octet-stream'
                filesize = fileinfo.info.size

                ranges = request.http_range
                offset = ranges.start or 0
                stop = ranges.stop or filesize
                size = stop - offset

                status = 200 if ranges.start is None and ranges.stop is None else 206
                rangestr = f'bytes {offset}-{stop-1}/{filesize}'

                resp = web.StreamResponse(
                    status=status,
                    headers={
                        'Accept-Ranges': 'bytes',
                        'Content-Type': mimetype,
                        'Content-Length': str(size),
                        'Content-Range': rangestr,
                        'Content-Disposition': f'inline; filename="{os.path.basename(action)}"'
                    }
                )
 
                if request.method == 'HEAD':
                    return resp

                await resp.prepare(request)

                streamer = StaticTorrentProducer(self, fileinfo, offset, size)

                try:
                    async for chunk in streamer:
                        await resp.write(chunk)
                        await resp.drain()                    # ← backpressure

                        # Если drain() долго ждёт — мы автоматически не запрашиваем новые pieces
                        # (это обеспечивается ленивым дизайном)

                except (ConnectionError, ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                    self.log.info("Client disconnected: %s", action)
                except Exception as e:
                    self.log.exception("Streaming error for %s", action)

                return resp

        return web.json_response(ret, dumps=lambda a: json.dumps(a).encode('utf8').decode('unicode-escape'))

