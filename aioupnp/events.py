#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# TODO: periodic subscribe

import asyncio
import logging
import aiohttp
import aiohttp.web
import urllib.parse
import xmltodict
import time
from . import notify

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
    def __init__(self, loop=None, http=None):
        self.log = logging.getLogger(self.__class__.__name__)
        self.loop = asyncio.get_event_loop() if loop is None else loop

        self.events = {}
        self.sidtourl = {}
        eventsapp = aiohttp.web.Application()
        eventsapp.router.add_route('*', '/', self.events_handler)
        http.add_subapp('/events/', eventsapp)

    async def events_handler(self, request):
        if request.has_body:
            body = await request.text()
            url = self.sidtourl.get(request.headers.get('SID'))
            self.log.warn('event %s %s', request.headers.get('SID'), url)
            #if url is None:
            print(body)
            events = self.events.setdefault(url, {})

            d = xmltodict.parse(body, dict_constructor=dict)
            lastchange = d.get('e:propertyset', {}).get('e:property', {}).get('LastChange')
            lastevents = xmltodict.parse(lastchange, dict_constructor=dict)
            eventsdict = lastevents.get('Event', {}).get('InstanceID', {})
            for var, data in eventsdict.items():
                if isinstance(data, str):
                    continue
                event = events.setdefault(var, Event(var))
                event.update(data.get('@val'))
            self.log.debug('events %s',self.events)
            notify.send('UPnP.DLNA.Event.{}'.format(request.headers.get('SID')), data=events)
        return aiohttp.web.Response()

    async def subscribe(self, url, lurl, callback):
        try:
            with aiohttp.ClientSession(read_timeout = 5) as session:
                async with session.request('SUBSCRIBE', url,
                    headers={
                        'TIMEOUT': 'Second-1800',
                        'CALLBACK': '<{}>'.format(urllib.parse.urljoin(lurl,'/events/')),
                        'NT': 'upnp:event',
                        'Date': time.ctime()
                    }
                    ) as resp:
                    if resp.status != 200:
                        raise aiohttp.client_exceptions.ClientResponseError('Error %d' % resp.status)
                    self.sidtourl[resp.headers.get('SID')] = url
                    notify.connect('UPnP.DLNA.Event.{}'.format(resp.headers.get('SID')), callback)
                    self.log.warn('subscribe %s %s', resp.headers.get('SID'), url)
                    return resp.headers.get('SID')
        except (OSError, asyncio.TimeoutError, aiohttp.client_exceptions.ClientError) as err:
            self.log.warn('%s: %s', err.__class__.__name__, err)

