#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
"""youtube-dl extraction: a cancellable multiprocessing pool and the Info wrapper around it."""

import asyncio
import logging
import multiprocessing

have_youtube_dl = False
try:
    import youtube_dl
    # delete generic extractor
    youtube_dl.extractor.gen_extractor_classes().remove(youtube_dl.extractor.generic.GenericIE)
    have_youtube_dl = True
except ModuleNotFoundError:
    pass


class CancellablePool:
    def __init__(self, max_workers=3):
        self._free = {self._new_pool() for _ in range(max_workers)}
        self._working = set()
        self._change = asyncio.Event()

    def _new_pool(self):
        return multiprocessing.Pool(1)

    async def apply(self, fn, *args):
        """
        Like multiprocessing.Pool.apply_async, but:
         * is an asyncio coroutine
         * terminates the process if cancelled
        """
        while not self._free:
            await self._change.wait()
            self._change.clear()
        pool = usable_pool = self._free.pop()
        self._working.add(pool)

        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        def _on_done(obj):
            loop.call_soon_threadsafe(fut.set_result, obj)
        def _on_err(err):
            loop.call_soon_threadsafe(fut.set_exception, err)
        pool.apply_async(fn, args, callback=_on_done, error_callback=_on_err)

        try:
            return await fut
        except asyncio.CancelledError:
            pool.terminate()
            usable_pool = self._new_pool()
        finally:
            self._working.remove(pool)
            self._free.add(usable_pool)
            self._change.set()

    def shutdown(self):
        for p in self._working | self._free:
            p.terminate()
        self._free.clear()


class Info:
    def __init__(self, loop):
        self.log = logging.getLogger(self.__class__.__name__)
        self.loop = loop

    @staticmethod
    def extract_info(url=None):
        try:
            ydl = youtube_dl.YoutubeDL(
                params={
                    'quiet': True,
                    'cachedir': '/tmp/',
                    'youtube_include_dash_manifest': False,
                    'prefer_ffmpeg': True,
                    'socket_timeout': 5,
                    'skip_download': True
                })
            stream = ydl.extract_info(url, False)
            data = {}
            data['src'] = stream.get('extractor')
            data['title'] = stream.get('title')
            data['url'] = stream.get('webpage_url')
            data['bitrate'] = []
            for i in stream.get('formats', []):
                if i.get('acodec') != 'none' and i.get('vcodec') != 'none':
                    data['bitrate'].append({'url':i.get('url'), 'bitrate':i.get('height') or i.get('format_id')})
            return data
        except Exception:
            pass

    async def youtube_dl(self, url):
        if not have_youtube_dl:
            return None
        pool = CancellablePool()
        task = self.loop.create_task(pool.apply(self.extract_info, url))
        try:
            return await asyncio.wait_for(task, 60)
        except Exception as exception:
            self.log.error('youtube_dl %s', exception)
        finally:
            pool.shutdown()
