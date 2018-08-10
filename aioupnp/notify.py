#!/usr/bin/env python3
# 
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#

import asyncio

class aioDispatcher:
    def __init__(self, loop=None):
        self.loop = asyncio.get_event_loop() if loop is None else loop
        self._pool = {}

    def connect(self, signal, callback):
        if callback not in self._pool.setdefault(signal, []):
            self._pool[signal].append(callback)

    def disconnect(self, signal, callback):
        if signal in self._pool and callback in self._pool[signal]:
            self._pool[signal].remove(callback)
            if not self._pool[signal]:
                self._pool.pop(signal)

    def send(self, signal, *args, **kwargs):
        if signal in self._pool:
            for callback in self._pool[signal]:
                self.loop.create_task(callback(*args, **kwargs))
                #asyncio.ensure_future(callback(*args, **kwargs))

global _global_dispatcher
_global_dispatcher = aioDispatcher()

def connect(signal, callback):
    _global_dispatcher.connect(signal, callback)

def disconnect(signal, callback):
    _global_dispatcher.disconnect(signal, callback)

def send(signal, *args, **kwargs):
    _global_dispatcher.send(signal, *args, **kwargs)
