#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# TODO: periodic subscribe

import asyncio
import logging
import aiohttp
import aiohttp.web
from typing import Type, Awaitable, Callable, Dict
import urllib.parse
import xmltodict
import time
from . import notify
from . import dlna

class Event:
    def __init__(self, name):
        self.name = name
        self.value = None
        self.old_value = None
        self.service = None

    def update(self, data):
        self.old_value = self.value
        self.value = data

    def __str__(self):
        return 'value {}, old_value {}'.format(self.value, self.old_value)

    def __repr__(self):
        return '{}[{}]'.format(self.name, self.value)

class EventsServer:
    def __init__(self, loop, http):
        self.log = logging.getLogger(self.__class__.__name__)
        self.loop = loop

        self.events = {}
        self.sidtoservice = {}
        self.running_tasks = {}
        eventsapp = aiohttp.web.Application()
        eventsapp.router.add_route('*', '/', self.events_handler)
        http.add_subapp('/events/', eventsapp)

    async def events_handler(self, request):
        self.log.debug('events_handler request %s', request)
        if request.has_body:
            body = await request.text()
            service = self.sidtoservice.get(request.headers.get('SID'))
            self.log.debug('event %s %s', request.headers.get('SID'), service)
            if service:
                events = self.events.setdefault(service.uid, {})

                d = xmltodict.parse(body, dict_constructor=dict)
                lastchange = d.get('e:propertyset', {}).get('e:property', {}).get('LastChange')
                if lastchange:
                    lastevents = xmltodict.parse(lastchange, dict_constructor=dict)
                    eventsdict = lastevents.get('Event', {}).get('InstanceID', {})
                    for var, data in eventsdict.items():
                        if isinstance(data, str):
                            continue
                        event = events.setdefault(var, Event(var))
                        event.update(data.get('@val'))
                    self.log.debug('events %s', self.events)
                    notify.send('UPnP.DLNA.Event.{}'.format(request.headers.get('SID')), data=events)
        return aiohttp.web.Response()

    async def subscribe(self, service: Type[dlna.DLNAService], callback: Awaitable[Callable[[Dict[str, Event]],None]]):
        self.running_tasks[service.uid] = self.loop.create_task(self.event_task(service, callback))

    async def unsubscribe(self, service: Type[dlna.DLNAService]):
        task = self.running_tasks.pop(service.uid)
        if task:
            try:
                task.cancel()
                await task
            except asyncio.CancelledError:
                pass

    #async def shutdown(self):
    #    for task in list(self.running_tasks.keys()):
    #        await self.unsubscribe(self.running_tasks[task])

    async def event_task(self, service, callback):
        while True:
            try:
                sid = None
                timeout = None
                async with aiohttp.ClientSession(read_timeout = 5, raise_for_status=True) as session:
                    try:
                        self.log.info('event_task %s callback %s', service.url, urllib.parse.urljoin(service.device.localhost, '/events/'))
                        async with session.request('SUBSCRIBE', service.url,
                            headers={
                                'TIMEOUT': 'Second-1800',
                                'CALLBACK': '<{}>'.format(urllib.parse.urljoin(service.device.localhost, '/events/')),
                                'NT': 'upnp:event',
                                'Date': time.ctime()
                            }) as resp:

                            sid = resp.headers.get('SID')
                            #TODO: parse Second-1800
                            timeout = int(''.join(filter(str.isdigit, resp.headers.get('TIMEOUT'))))
                            self.sidtoservice[sid] = service
                            notify.connect('UPnP.DLNA.Event.{}'.format(sid), callback)
                            self.log.warn('subscribe %s[%s] event SID:%s', service.device.friendlyName, service.serviceType, sid)
                        while True:
                            await asyncio.sleep(timeout/2)
                            async with session.request('SUBSCRIBE', service.url,
                                headers={
                                    'SID': sid,
                                }) as resp:
                                self.log.warn('resubscribe %s[%s] event SID:%s', service.device.friendlyName, service.serviceType, resp.headers.get('SID'))
                    finally:
                        notify.disconnect('UPnP.DLNA.Event.{}'.format(sid), callback)
                        if sid in self.sidtoservice:
                            service = self.sidtoservice.pop(sid)
                            if service.uid in self.events:
                                self.events.pop(service.uid)
                        async with session.request('UNSUBSCRIBE', service.url,
                            headers={
                                'SID': sid,
                            }) as resp:
                                self.log.warn('unsubscribe %s %s', resp, service.url)

            except (OSError, asyncio.TimeoutError, aiohttp.client_exceptions.ClientError, aiohttp.client_exceptions.ClientResponseError) as err:
                self.log.warn('event_task %s: %s', err.__class__.__name__, err)
            await asyncio.sleep(60)

