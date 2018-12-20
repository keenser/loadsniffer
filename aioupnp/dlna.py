#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#

import logging
import xmltodict
import lxml.etree as xml
import urllib.parse
import aiohttp
import asyncio

n = lambda n, e: "{{{}}}{}".format(DIDLLite.namespaces[n], e)

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

    def __init__(self):
        for i, j in self.namespaces.items():
            xml.register_namespace(i, j)

    def DIDLElement(self, item):
        element = xml.Element('DIDL-Lite', xmlns='urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/')
        element.append(item)
        return element

    def VideoItem(self, itemid, parentid, restricted, title, resource):
        item = xml.Element('item', {'id': str(itemid) , 'parentID': str(parentid), 'restricted': str(restricted)})
        _title = xml.SubElement(item, n('dc', 'title'))
        _title.text = title
        _class = xml.SubElement(item, n('upnp', 'class'))
        _class.text = 'object.item.videoItem'
        _date = xml.SubElement(item, n('dc', 'date'))
        _date.text = '2003-07-23T01:18:00+02:00'
        item.append(resource)
        return item

    def Resource(self, protocolInfo, text):
        _resource = xml.Element('res', protocolInfo=protocolInfo)
        _resource.text = text
        return _resource

    def toString(self, data):
        return xml.tostring(data, encoding='utf8', xml_declaration=True).decode()

    def fromString(self, data):
        return xmltodict.parse(data, dict_constructor=dict)

didl = DIDLLite()

class DLNAAction:
    def __init__(self, service, action):
        self.service = service
        self.action = action

    async def call(self, **data):
        url = urllib.parse.urljoin(self.service.location, self.service.get('controlURL'))
        servicetype = self.service.get('serviceType')

        ns = {'s': 'http://schemas.xmlsoap.org/soap/envelope/'}
        n = lambda n, e: xml.QName(ns[n], e)
        e = xml.Element(n('s','Envelope'), attrib={n('s', 'encodingStyle'): "http://schemas.xmlsoap.org/soap/encoding/"}, nsmap=ns)
        b = xml.SubElement(e, n('s','Body'))
        a = xml.SubElement(b, xml.QName(servicetype, self.action), nsmap={'u': servicetype})
        for name, val in data.items():
            i = xml.SubElement(a, name)
            i.text = str(val)

        datastr = xml.tostring(e, encoding='utf8', xml_declaration=True, pretty_print=True).decode()

        async with aiohttp.ClientSession(read_timeout = 5, raise_for_status=True) as session:
            async with session.post(url, data=datastr,
                headers={
                    'SOAPACTION':'"{}#{}"'.format(servicetype, self.action),
                    'content-type': 'text/xml; charset="utf-8"'
                }
                ) as resp:
                return resp


class DLNAService(dict):
    def __init__(self, device, service):
        super().__init__(service)
        self.device = device
        #self.lastevents = {}
        self.events_subscription = False
        self.callbacks = {}

    def action(self, action):
        return DLNAAction(self, action)

    async def shutdown(self):
        if self.events_subscription:
            #url = urllib.parse.urljoin(self.location, self.get('eventSubURL'))
            await self.device.events.unsubscribe(self)

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
            #url = urllib.parse.urljoin(self.location, self.get('eventSubURL'))
            await self.device.events.subscribe(self, self.eventscallback)

    async def resubscribe(self):
        if self.events_subscription:
            await self.device.events.unsubscribe(self)
            await self.device.events.subscribe(self, self.eventscallback)

    @property
    def location(self):
        return self.device.location

    @property
    def serviceType(self):
        return self.get('serviceType')

    @property
    def url(self):
        return urllib.parse.urljoin(self.location, self.get('eventSubURL'))

    @property
    def uid(self):
        return '{}:{}'.format(self.device.usn, self.serviceType)


class AVTransport(DLNAService):
    async def stop(self):
        return await self.action('Stop').call(InstanceID=0)

    async def play(self):
        return await self.action('Play').call(InstanceID=0, Speed=1)

    async def transporturi(self, url, title, mime):
        dlnatags = 'http-get:*:{}:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000'.format(mime)
        metadata = didl.DIDLElement(didl.VideoItem(None, None, 0, title, didl.Resource(dlnatags, url)))
        return await self.action('SetAVTransportURI').call(InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl.toString(metadata))

