#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#

import logging
import xmltodict
import xml.etree.ElementTree as xml
import urllib.parse
import aiohttp
import asyncio

class DIDLLite(dict):
    namespaces = {
        'upnp': 'urn:schemas-upnp-org:metadata-1-0/upnp/',
        'e': 'urn:schemas-upnp-org:event-1-0',
        'd': 'urn:schemas-upnp-org:device-1-0',
        's': 'urn:schemas-upnp-org:service-1-0',
        'dlna': 'urn:schemas-dlna-org:metadata-1-0',
        'dev': 'urn:schemas-dlna-org:device-1-0',
        'dc': 'http://purl.org/dc/elements/1.1/',
        'pv': 'http://www.pv.com/pvns/',
    }

    def DIDLElement(self, item):
        element = {'DIDL-Lite': {
            '@xmlns:dc': 'http://purl.org/dc/elements/1.1/',
            '@xmlns:upnp': 'urn:schemas-upnp-org:metadata-1-0/upnp/',
            '@xmlns': 'urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/'
        }}
        element['DIDL-Lite'].update(item)
        return element

    def VideoItem(self, itemid, patentid, restricted, title, resource):
        item = {
            'item': {
                '@id': itemid,
                '@parentID': patentid,
                '@restricted': restricted,
                'dc:title': {
                    '@xmlns:dc': 'http://purl.org/dc/elements/1.1/'
                },
                'upnp:class': {
                    '@xmlns:upnp': 'urn:schemas-upnp-org:metadata-1-0/upnp/',
                    '#text': 'object.item.videoItem'
                },
                'dc:date': {
                    '@xmlns:dc': 'http://purl.org/dc/elements/1.1/',
                    '#text': '2003-07-23T01:18:00+02:00'
                }
            }
        }
        item['item'].update(resource)
        return item

    def Resource(self, protocolInfo, text):
        return {
            'res': {
                '@protocolInfo': protocolInfo,
                '#text': text
            }
        }
    def toString(self, data):
        return xmltodict.unparse(data)

didl = DIDLLite()

class DLNAAction:
    def __init__(self, service, action):
        self.service = service
        self.action = action

    async def call(self, **data):
        url = urllib.parse.urljoin(self.service.location, self.service.get('controlURL'))
        servicetype = self.service.get('serviceType')
        data['@xmlns:u'] = servicetype
        payload = {
                's:Envelope':{
                    '@xmlns:s': 'http://schemas.xmlsoap.org/soap/envelope/',
                    '@s:encodingStyle': 'http://schemas.xmlsoap.org/soap/encoding/',
                    's:Body': {
                        'u:{}'.format(self.action): data
                        }
                    }
                }
        with aiohttp.ClientSession(read_timeout = 5) as session:
            async with session.post(url, data=xmltodict.unparse(payload), 
                headers={
                    'SOAPACTION':'"{}#{}"'.format(servicetype, self.action),
                    'content-type': 'text/xml; charset="utf-8"'
                }
                ) as resp:
                return resp


class DLNAService(dict):
    def __init__(self, location, service, device):
        super().__init__(service)
        self.location = location
        self.device = device
        #self.lastevents = {}
        self.events_subscription = False
        self.callbacks = {}

    def action(self, action):
        return DLNAAction(self, action)

    async def eventscallback(self, data):
        #d = xmltodict.parse(data, dict_constructor=dict)
        #lastevent = d.get('e:propertyset', {}).get('e:property', {}).get('LastChange')
        #self.lastevents = xmltodict.parse(lastevent, dict_constructor=dict)
        for variable, callback in self.callbacks.items():
            #callback(self.lastevents.get('Event', {}).get('InstanceID', {}).get(variable, {}).get('@val'))
            event = data.get(variable)
            event.service = self
            callback(event)

    async def subscribe(self, variable, callback):
        self.callbacks[variable] = callback
        if self.events_subscription == False:
            self.events_subscription = True
            url = urllib.parse.urljoin(self.location, self.get('eventSubURL'))
            await self.device.events.subscribe(url, self.device.description.get('localhost'), self.eventscallback)


class AVTransport(DLNAService):
    def __init__(self, location, service, device):
        super().__init__(location, service, device)

    async def stop(self):
        return await self.action('Stop').call(InstanceID=0)

    async def play(self):
        return await self.action('Play').call(InstanceID=0, Speed=1)

    async def transporturi(self, url, title, mime):
        dlnatags = 'http-get:*:{}:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000'.format(mime)
        metadata = didl.DIDLElement(didl.VideoItem(None, None, 0, title, didl.Resource(dlnatags, url)))
        return await self.action('SetAVTransportURI').call(InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl.toString(metadata))

