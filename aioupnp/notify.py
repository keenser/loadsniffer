#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#

import asyncio


class AioDispatcher:
    """simple asyncio send/notify module"""

    def __init__(self, loop=None):
        self.loop = loop or asyncio.get_event_loop()
        self._pool = {}

    def connect(self, signal, callback):
        """actual callback connect to signal"""
        if callback not in self._pool.setdefault(signal, []):
            self._pool[signal].append(callback)

    def disconnect(self, signal, callback):
        """actual remove callback from signal"""
        if signal in self._pool and callback in self._pool[signal]:
            self._pool[signal].remove(callback)
            if not self._pool[signal]:
                self._pool.pop(signal)

    def send(self, signal, *args, **kwargs):
        """actual trigger connected to signal callbacks"""
        if signal in self._pool:
            for callback in self._pool[signal]:
                self.loop.create_task(callback(*args, **kwargs))


global _global_dispatcher
_global_dispatcher = AioDispatcher()


def connect(signal, callback):
    """add callback to signal"""
    _global_dispatcher.connect(signal, callback)


def disconnect(signal, callback):
    """remove callback from signal"""
    _global_dispatcher.disconnect(signal, callback)


def send(signal, *args, **kwargs):
    """trigger connected to signal callbacks"""
    _global_dispatcher.send(signal, *args, **kwargs)
