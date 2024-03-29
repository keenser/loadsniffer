#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#

import logging
import urllib.parse
import aiohttp
import lxml.etree as xml


class Element(xml.ElementBase):
    def find(self, match, namespaces=None):
        return super().find(match, namespaces or self.nsmap)

    def findall(self, match, namespaces=None):
        return super().findall(match, namespaces or self.nsmap)

    def findtext(self, match, default=None, namespaces=None):
        return super().findtext(match, default, namespaces or self.nsmap)

    def iterfind(self, match, namespaces=None):
        return super().iterfind(match, namespaces or self.nsmap)

    def get(self, match):
        element = self.find(match)
        return element.text if element is not None else None


class DIDLLite:
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

        lookup = xml.ElementDefaultClassLookup(element=Element)
        self.parser = xml.XMLParser(encoding='utf8')
        self.parser.set_element_class_lookup(lookup)

    @staticmethod
    def DIDLElement(item):
        element = xml.Element('DIDL-Lite', xmlns='urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/')
        element.append(item)
        return element

    @staticmethod
    def VideoItem(itemid, parentid, restricted, title, resource):
        n = lambda n, e: xml.QName(DIDLLite.namespaces[n], e)

        item = xml.Element('item', {'id': str(itemid), 'parentID': str(parentid), 'restricted': str(restricted)})
        _title = xml.SubElement(item, n('dc', 'title'))
        _title.text = title
        _class = xml.SubElement(item, n('upnp', 'class'))
        _class.text = 'object.item.videoItem'
        _date = xml.SubElement(item, n('dc', 'date'))
        _date.text = '2003-07-23T01:18:00+02:00'
        item.append(resource)
        return item

    @staticmethod
    def Resource(protocolInfo, text):
        _resource = xml.Element('res', protocolInfo=protocolInfo)
        _resource.text = text
        return _resource

    @staticmethod
    def toString(data):
        return xml.tostring(data, encoding='utf8', xml_declaration=True).decode()

    def fromString(self, data):
        return xml.fromstring(data, self.parser)


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

        e = xml.Element(n('s', 'Envelope'), attrib={n('s', 'encodingStyle'): "http://schemas.xmlsoap.org/soap/encoding/"}, nsmap=ns)
        b = xml.SubElement(e, n('s', 'Body'))
        a = xml.SubElement(b, xml.QName(servicetype, self.action), nsmap={'u': servicetype})
        for name, val in data.items():
            xml.SubElement(a, name).text = str(val)

        datastr = xml.tostring(e, encoding='utf8', xml_declaration=True, pretty_print=False).decode()

        async with aiohttp.ClientSession(read_timeout=5, raise_for_status=True) as session:
            async with session.post(url, data=datastr,
                        headers={
                            'SOAPACTION':'"{}#{}"'.format(servicetype, self.action),
                            'content-type': 'text/xml; charset="utf-8"'
                        }
                    ) as resp:
                return resp


class DLNAService:
    def __init__(self, device, service):
        self.service = service
        self.device = device
        self.events_subscription = False
        self.callbacks = {}

    def action(self, action):
        return DLNAAction(self, action)

    async def shutdown(self):
        if self.events_subscription:
            await self.device.events.unsubscribe(self.uid)

    async def eventscallback(self, data):
        for variable, callback in self.callbacks.items():
            event = data.get(variable)
            event.service = self
            callback(event)

    def subscribe(self, variable, callback):
        self.callbacks[variable] = callback
        if self.events_subscription is False:
            self.events_subscription = True
            self.device.events.subscribe(self, self.eventscallback)

    async def resubscribe(self):
        if self.events_subscription:
            await self.device.events.unsubscribe(self.uid)
            self.device.events.subscribe(self, self.eventscallback)

    def get(self, name):
        return self.service.get(name)

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

    @property
    def friendlyName(self):
        return '{}:{}'.format(self.device.friendlyName, self.device.getName(self.serviceType))


class AVTransport(DLNAService):
    async def stop(self):
        return await self.action('Stop').call(InstanceID=0)

    async def play(self):
        return await self.action('Play').call(InstanceID=0, Speed=1)

    async def pause(self):
        return await self.action('Pause').call(InstanceID=0)

    async def setplaymode(self, mode):
        return await self.action('SetPlayMode').call(InstanceID=0, NewPlayMode=mode)

    async def setavtransporturi(self, url, title, mime):
        dlnatags = 'http-get:*:{}:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000'.format(mime)
        metadata = didl.DIDLElement(didl.VideoItem(None, None, 0, title, didl.Resource(dlnatags, url)))
        return await self.action('SetAVTransportURI').call(InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl.toString(metadata))

    async def setnextavtransporturi(self, url, title, mime):
        dlnatags = 'http-get:*:{}:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000'.format(mime)
        metadata = didl.DIDLElement(didl.VideoItem(None, None, 0, title, didl.Resource(dlnatags, url)))
        return await self.action('SetNextAVTransportURI').call(InstanceID=0, NextURI=url, NextURIMetaData=didl.toString(metadata))
