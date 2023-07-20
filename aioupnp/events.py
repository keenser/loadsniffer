#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# TODO: periodic subscribe

from __future__ import annotations
import asyncio
import logging
import aiohttp
import aiohttp.web
import aiohttp.client_exceptions
from typing import Optional, Type, Awaitable, Callable, Dict
import urllib.parse
import lxml.etree as xml
import time
from . import notify
from . import dlna


class Event:
    def __init__(self, name:str):
        self.name = name
        self.value = None
        self.old_value = None
        self.service: Optional[dlna.DLNAService] = None

    def update(self, data:str):
        self.old_value = self.value
        self.value = data

    def __str__(self):
        return 'value {}, old_value {}'.format(self.value, self.old_value)

    def __repr__(self):
        return '{}[{}]'.format(self.name, self.value)


class EventsServer:
    def __init__(self, loop:asyncio.AbstractEventLoop, http:aiohttp.web.Application):
        self.log = logging.getLogger('{}.{}'.format(__name__, self.__class__.__name__))
        self.loop = loop

        self.events = {}
        self.sidtoservice: Dict[str, dlna.DLNAService] = {}
        self.running_tasks = {}
        eventsapp = aiohttp.web.Application()
        eventsapp.router.add_route('*', '/', self.events_handler)
        http.add_subapp('/events/', eventsapp)
        http.on_shutdown.append(self.shutdown)

    async def events_handler(self, request:aiohttp.web.Request) -> aiohttp.web.StreamResponse:
        if request.can_read_body:
            body = await request.read()
            service = self.sidtoservice.get(request.headers['SID'])
            self.log.debug('event %s %s', request.headers['SID'], service)
            if service:
                events = self.events.setdefault(service.uid, {})

                d = xml.fromstring(body, parser=None)
                lastchange = d.find('e:property/LastChange', d.nsmap)
                if lastchange is not None:
                    lastevents = xml.fromstring(lastchange.text, parser=None)
                    for lastevent in lastevents.iterfind('InstanceID/*', lastevents.nsmap):
                        tag = xml.QName(lastevent.tag)
                        event = events.setdefault(tag.localname, Event(tag.localname))
                        event.update(lastevent.attrib['val'])
                    self.log.debug('events %s', self.events)
                    notify.send('UPnP.DLNA.Event.{}'.format(request.headers.get('SID')), data=events)

        return aiohttp.web.Response()

    async def shutdown(self, app=None):
        self.log.info('shutdown')
        for uid in list(self.running_tasks.keys()):
            await self.unsubscribe(uid)

    def subscribe(self, service:dlna.DLNAService, callback:Callable[[Dict[str, Event]], Awaitable[None]]) -> None:
        self.running_tasks[service.uid] = self.loop.create_task(self._event_task(service, callback))

    async def unsubscribe(self, uid:str) -> None:
        if uid in self.running_tasks:
            task = self.running_tasks.pop(uid)
            if task:
                try:
                    task.cancel()
                    await task
                except asyncio.CancelledError:
                    pass
                self.log.info('task.cancel done %s', uid)

    async def _event_task(self, service:dlna.DLNAService, callback:Callable[[Dict[str, Event]], Awaitable[None]]):
        async with aiohttp.ClientSession(read_timeout=5, raise_for_status=True) as session:
            sid = None
            try:
                while True:
                    try:
                        self.log.info('event_task %s callback %s', service.friendlyName,
                                      urllib.parse.urljoin(service.device.localhost, '/events/'))
                        async with session.request('SUBSCRIBE', service.url,
                                                   headers={
                                                       'TIMEOUT': 'Second-1800',
                                                       'CALLBACK': '<{}>'.format(
                                                           urllib.parse.urljoin(service.device.localhost, '/events/')),
                                                       'NT': 'upnp:event',
                                                       'Date': time.ctime()
                                                   }) as resp:

                            sid = resp.headers['SID']
                            # TODO: parse Second-1800
                            timeout = int(''.join(filter(str.isdigit, resp.headers['TIMEOUT'])))
                            self.sidtoservice[sid] = service
                            notify.connect('UPnP.DLNA.Event.{}'.format(sid), callback)
                            self.log.warning('subscribe %s event SID:%s', service.friendlyName, sid)
                        while True:
                            await asyncio.sleep(timeout / 2)
                            async with session.request('SUBSCRIBE', service.url,
                                                       headers={
                                                           'SID': sid,
                                                       }) as resp:
                                self.log.warning('resubscribe %s event SID:%s', service.friendlyName,
                                                 resp.headers.get('SID'))
                    except (OSError, asyncio.TimeoutError, aiohttp.client_exceptions.ClientError,
                            aiohttp.client_exceptions.ClientResponseError) as err:
                        self.log.warning('event_task %s %s', err.__class__.__name__, service.friendlyName)
                    finally:
                        notify.disconnect('UPnP.DLNA.Event.{}'.format(sid), callback)
                        if sid in self.sidtoservice:
                            service = self.sidtoservice.pop(sid)
                            if service.uid in self.events:
                                self.events.pop(service.uid)
                    await asyncio.sleep(60)
            finally:
                if sid:
                    try:
                        async with session.request('UNSUBSCRIBE', service.url, headers={'SID': sid}) as resp:
                            self.log.warning('unsubscribe %s retcode: %s', service.friendlyName, resp.status)
                    except (OSError, asyncio.TimeoutError, aiohttp.client_exceptions.ClientError,
                            aiohttp.client_exceptions.ClientResponseError):
                        pass
