#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
"""UPnP MediaServer (ContentDirectory) so DLNA clients can browse served files directly."""

# NOTE: no `from __future__ import annotations` here - async_upnp_client.server inspects
# @callable_action methods' *real* runtime type objects (str/int) to match them against
# declared state variables; postponed (string) annotations break that check.
import asyncio
import logging
import socket
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from didl_lite import didl_lite
from pydantic import BaseModel
from async_upnp_client.const import DeviceInfo, HttpRequest, ServiceInfo
from async_upnp_client.exceptions import UpnpConnectionError
from async_upnp_client.server import (
    EventSubscriber,
    UpnpEventableStateVariable,
    UpnpServer,
    UpnpServerDevice,
    UpnpServerService,
    callable_action,
    create_event_var,
    create_state_var,
)

_ITEM_CLASS_BY_MAJOR_MIME = {
    'video': didl_lite.VideoItem,
    'audio': didl_lite.AudioItem,
    'image': didl_lite.ImageItem,
}


class Container(BaseModel):
    id: str
    title: str


class Item(BaseModel):
    id: int
    title: str
    url: str
    mime: Optional[str] = None


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('0.0.0.0', 0))
        return probe.getsockname()[1]


class ContentDirectoryService(UpnpServerService):
    SERVICE_DEFINITION = ServiceInfo(
        service_id='urn:upnp-org:serviceId:ContentDirectory',
        service_type='urn:schemas-upnp-org:service:ContentDirectory:1',
        control_url='/ContentDirectory/control',
        event_sub_url='/ContentDirectory/event',
        scpd_url='/ContentDirectory/scpd.xml',
        xml=ET.Element('service'),
    )

    def add_subscriber(self, subscriber: EventSubscriber) -> None:
        """Log and register a subscription from a DLNA client (e.g. a TV)."""
        super().add_subscriber(subscriber)
        logging.getLogger('aioupnp.mediaserver').info(
            'ContentDirectory: DLNA client subscribed to events '
            'callback=%s sid=%s timeout=%ss',
            subscriber.url,
            subscriber.uuid,
            subscriber.timeout,
        )

    def del_subscriber(self, sid: str) -> bool:
        """Log and remove a subscription."""
        removed = super().del_subscriber(sid)
        if removed:
            logging.getLogger('aioupnp.mediaserver').info(
                'ContentDirectory: DLNA client unsubscribed from events sid=%s',
                sid,
            )
        return removed

    async def async_send_events(self, subscriber: EventSubscriber | None = None) -> None:
        """Send events to subscribers, tolerating unreachable callbacks.

        The base implementation uses ``asyncio.gather()`` without
        ``return_exceptions``, so one dead callback (e.g. a client that
        subscribed but whose NOTIFY port no longer listens) makes the whole
        delivery task raise and spam the log. Here each subscriber is delivered
        independently: a failure is logged and the dead subscription is dropped
        instead of breaking delivery to everyone else (like the TV).
        """
        logger = logging.getLogger('aioupnp.mediaserver')
        if not subscriber:
            # EventSubscriber.expiration uses offset-naive datetime.now(), so
            # the comparison must use a naive timestamp too.
            now = datetime.now()
            subscribers = [sub for sub in self._subscribers if now < sub.expiration]
            self._subscribers = subscribers
            if not subscribers:
                return
        else:
            subscribers = [subscriber]

        event_el = ET.Element('e:propertyset')
        event_el.set('xmlns:e', 'urn:schemas-upnp-org:event-1-0')
        for state_var in self.state_variables.values():
            if not isinstance(state_var, UpnpEventableStateVariable):
                continue
            prop_el = ET.SubElement(event_el, 'e:property')
            ET.SubElement(prop_el, state_var.name).text = str(state_var.value)
        message = ET.tostring(event_el, encoding='utf-8', xml_declaration=True).decode()

        headers = {
            'CONTENT-TYPE': 'text/xml; charset="utf-8"',
            'NT': 'upnp:event',
            'NTS': 'upnp:propchange',
        }

        async def _deliver(sub: EventSubscriber) -> None:
            hdr = headers.copy()
            hdr['SID'] = sub.uuid
            hdr['SEQ'] = str(sub.get_next_seq())
            await self.requester.async_http_request(
                HttpRequest('NOTIFY', sub.url, headers=hdr, body=message)
            )

        results = await asyncio.gather(
            *(_deliver(sub) for sub in subscribers),
            return_exceptions=True,
        )
        for sub, result in zip(subscribers, results):
            if result is None:
                continue
            if isinstance(result, UpnpConnectionError):
                logger.warning(
                    'ContentDirectory: dropping subscription to unreachable callback '
                    'callback=%s sid=%s err=%s',
                    sub.url,
                    sub.uuid,
                    result,
                )
                self.del_subscriber(sub.uuid)
            else:
                logger.error(
                    'ContentDirectory: failed to notify callback=%s sid=%s err=%s',
                    sub.url,
                    sub.uuid,
                    result,
                )

    STATE_VARIABLE_DEFINITIONS = {
        'A_ARG_TYPE_ObjectID': create_state_var('string'),
        'A_ARG_TYPE_Result': create_state_var('string'),
        'A_ARG_TYPE_BrowseFlag': create_state_var('string'),
        'A_ARG_TYPE_Filter': create_state_var('string'),
        'A_ARG_TYPE_SortCriteria': create_state_var('string'),
        'A_ARG_TYPE_Index': create_state_var('ui4'),
        'A_ARG_TYPE_Count': create_state_var('ui4'),
        'A_ARG_TYPE_UpdateID': create_state_var('ui4'),
        'SearchCapabilities': create_state_var('string', default=''),
        'SortCapabilities': create_state_var('string', default=''),
        'SystemUpdateID': create_event_var('ui4', default='0', max_rate=1.0),
        'ContainerUpdateIDs': create_event_var('string', default='', max_rate=1.0),
    }

    # Bound to real callables by MediaServer, per instance of the server.
    _list_containers: Callable[[], List[Container]] = staticmethod(lambda: [])
    _list_items: Callable[[str], List[Item]] = staticmethod(lambda container_id: [])

    @callable_action(
        'Browse',
        in_args={
            'ObjectID': 'A_ARG_TYPE_ObjectID',
            'BrowseFlag': 'A_ARG_TYPE_BrowseFlag',
            'Filter': 'A_ARG_TYPE_Filter',
            'StartingIndex': 'A_ARG_TYPE_Index',
            'RequestedCount': 'A_ARG_TYPE_Count',
            'SortCriteria': 'A_ARG_TYPE_SortCriteria',
        },
        out_args={
            'Result': 'A_ARG_TYPE_Result',
            'NumberReturned': 'A_ARG_TYPE_Count',
            'TotalMatches': 'A_ARG_TYPE_Count',
            'UpdateID': 'A_ARG_TYPE_UpdateID',
        },
    )
    async def browse(self, ObjectID: str, BrowseFlag: str, Filter: str,
                      StartingIndex: int, RequestedCount: int, SortCriteria: str) -> Dict[str, object]:
        if BrowseFlag == 'BrowseMetadata':
            obj = self._object(ObjectID)
            objects = [obj] if obj is not None else []
            total = len(objects)
        else:
            objects = self._children(ObjectID)
            total = len(objects)
            end = StartingIndex + RequestedCount if RequestedCount else None
            objects = objects[StartingIndex:end]

        return {
            'Result': didl_lite.to_xml_string(*objects).decode(),
            'NumberReturned': len(objects),
            'TotalMatches': total,
            'UpdateID': self.state_variable('SystemUpdateID').value,
        }

    @callable_action('GetSearchCapabilities', in_args={}, out_args={'SearchCaps': 'SearchCapabilities'})
    async def get_search_capabilities(self) -> Dict[str, object]:
        return {'SearchCaps': ''}

    @callable_action('GetSortCapabilities', in_args={}, out_args={'SortCaps': 'SortCapabilities'})
    async def get_sort_capabilities(self) -> Dict[str, object]:
        return {'SortCaps': ''}

    @callable_action('GetSystemUpdateID', in_args={}, out_args={'Id': 'SystemUpdateID'})
    async def get_system_update_id(self) -> Dict[str, object]:
        return {'Id': self.state_variable('SystemUpdateID').value}

    def bump_update_id(self) -> None:
        """Bump SystemUpdateID and ContainerUpdateIDs.

        Both are evented state variables; updating them triggers NOTIFY to any
        subscribed DLNA client, telling it the content tree changed and it
        should refresh its listing.
        """
        system_update_id = (self.state_variable('SystemUpdateID').value or 0) + 1
        self.state_variable('SystemUpdateID').value = system_update_id

        # ContainerUpdateIDs is a CSV list of "ContainerID,UpdateID" pairs for
        # the containers that changed. We bump every known container (plus the
        # root "0") to the same value as SystemUpdateID.
        container_ids = ['0']
        for container in self._list_containers():
            cid = container.get('id')
            if cid is not None:
                container_ids.append(str(cid))
        container_update_ids = ','.join(
            '{},{}'.format(cid, system_update_id) for cid in container_ids
        )
        self.state_variable('ContainerUpdateIDs').value = container_update_ids

        logging.getLogger('aioupnp.mediaserver').info(
            'ContentDirectory: content changed, SystemUpdateID=%s ContainerUpdateIDs=%s '
            'subscribers=%d',
            system_update_id,
            container_update_ids,
            len(self._subscribers),
        )

    def _children(self, object_id: str) -> List[didl_lite.DidlObject]:
        if object_id == '0':
            return [
                didl_lite.Container(id=container.id, parent_id='0', title=container.title, restricted='1')
                for container in self._list_containers()
            ]
        return [self._to_didl_item(object_id, item) for item in self._list_items(object_id)]

    def _object(self, object_id: str) -> Optional[didl_lite.DidlObject]:
        if object_id == '0':
            return didl_lite.Container(id='0', parent_id='-1', title='loadsniffer', restricted='1')

        for container in self._list_containers():
            if container.id == object_id:
                return didl_lite.Container(id=container.id, parent_id='0', title=container.title, restricted='1')

        container_id, _, _ = object_id.partition('/')
        for item in self._list_items(container_id):
            if '{}/{}'.format(container_id, item.id) == object_id:
                return self._to_didl_item(container_id, item)
        return None

    # DLNA.ORG_OP=01 advertises time-based positioning/seek to the renderer;
    # without it seek/rewind is disabled on many devices (e.g. LG WebOS) even
    # when browsing the MediaServer. Matches the metadata used by
    # UPnPctrl.transporturi() so playback behaves the same either way.
    _DLNA_FEATURES = 'DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000'

    @staticmethod
    def _to_didl_item(container_id: str, item: Item) -> didl_lite.Item:
        mime = item.mime or 'application/octet-stream'
        item_cls = _ITEM_CLASS_BY_MAJOR_MIME.get(mime.split('/')[0], didl_lite.Item)
        protocol_info = 'http-get:*:{}:*'.format(mime)
        if mime.startswith('video/'):
            protocol_info = 'http-get:*:{}:{}'.format(mime, ContentDirectoryService._DLNA_FEATURES)
        resource = didl_lite.Resource(uri=item.url, protocol_info=protocol_info)
        return item_cls(
            id='{}/{}'.format(container_id, item.id),
            parent_id=container_id,
            title=item.title,
            restricted='1',
            res=[resource],
        )


class ConnectionManagerService(UpnpServerService):
    """Minimal, required-by-spec stub: no real connection tracking is needed for HTTP GET streaming."""

    SERVICE_DEFINITION = ServiceInfo(
        service_id='urn:upnp-org:serviceId:ConnectionManager',
        service_type='urn:schemas-upnp-org:service:ConnectionManager:1',
        control_url='/ConnectionManager/control',
        event_sub_url='/ConnectionManager/event',
        scpd_url='/ConnectionManager/scpd.xml',
        xml=ET.Element('service'),
    )
    STATE_VARIABLE_DEFINITIONS = {
        'SourceProtocolInfo': create_state_var('string', default='http-get:*:*:*'),
        'SinkProtocolInfo': create_state_var('string', default=''),
        'CurrentConnectionIDs': create_state_var('string', default='0'),
        'A_ARG_TYPE_ConnectionStatus': create_state_var('string', default='OK'),
        'A_ARG_TYPE_ConnectionManager': create_state_var('string', default=''),
        'A_ARG_TYPE_Direction': create_state_var('string', default='Output'),
        'A_ARG_TYPE_ProtocolInfo': create_state_var('string', default=''),
        'A_ARG_TYPE_ConnectionID': create_state_var('i4', default='-1'),
        'A_ARG_TYPE_AVTransportID': create_state_var('i4', default='-1'),
        'A_ARG_TYPE_RcsID': create_state_var('i4', default='-1'),
    }

    @callable_action('GetProtocolInfo', in_args={}, out_args={'Source': 'SourceProtocolInfo', 'Sink': 'SinkProtocolInfo'})
    async def get_protocol_info(self) -> Dict[str, object]:
        return {'Source': self.state_variable('SourceProtocolInfo').value, 'Sink': ''}

    @callable_action('GetCurrentConnectionIDs', in_args={}, out_args={'ConnectionIDs': 'CurrentConnectionIDs'})
    async def get_current_connection_ids(self) -> Dict[str, object]:
        return {'ConnectionIDs': '0'}

    @callable_action(
        'GetCurrentConnectionInfo',
        in_args={'ConnectionID': 'A_ARG_TYPE_ConnectionID'},
        out_args={
            'RcsID': 'A_ARG_TYPE_RcsID',
            'AVTransportID': 'A_ARG_TYPE_AVTransportID',
            'ProtocolInfo': 'A_ARG_TYPE_ProtocolInfo',
            'PeerConnectionManager': 'A_ARG_TYPE_ConnectionManager',
            'PeerConnectionID': 'A_ARG_TYPE_ConnectionID',
            'Direction': 'A_ARG_TYPE_Direction',
            'Status': 'A_ARG_TYPE_ConnectionStatus',
        },
    )
    async def get_current_connection_info(self, ConnectionID: int) -> Dict[str, object]:
        return {
            'RcsID': -1,
            'AVTransportID': -1,
            'ProtocolInfo': '',
            'PeerConnectionManager': '',
            'PeerConnectionID': -1,
            'Direction': 'Output',
            'Status': 'OK',
        }


class _Server(UpnpServer):
    """async_upnp_client.server.UpnpServer keeps the created device private; expose it."""

    @property
    def device(self) -> Optional[UpnpServerDevice]:
        return self._device


class MediaServer:
    """Advertises a UPnP MediaServer with a ContentDirectory backed by the given callables."""

    def __init__(self,
                 list_containers: Callable[[], List[Container]],
                 list_items: Callable[[str], List[Item]],
                 friendly_name: str = 'loadsniffer',
                 http_port: Optional[int] = None,
                 source: Optional[Tuple[str, int]] = None
                 ) -> None:
        """`source`: (interface_ip, port) to bind the HTTP+SSDP server to.

        Also becomes the host in the advertised LOCATION/description URLs, so
        '0.0.0.0' (the default if left unset) would advertise an unreachable
        address to DLNA clients - pass the host's real LAN IP explicitly.
        """
        self.log = logging.getLogger('{}.{}'.format(__name__, self.__class__.__name__))

        udn = 'uuid:{}'.format(uuid.uuid5(uuid.NAMESPACE_DNS, 'loadsniffer-mediaserver-{}'.format(socket.gethostname())))
        device_info = DeviceInfo(
            device_type='urn:schemas-upnp-org:device:MediaServer:1',
            friendly_name=friendly_name,
            manufacturer='loadsniffer',
            manufacturer_url=None,
            model_description=None,
            model_name='loadsniffer',
            model_number=None,
            model_url=None,
            serial_number=None,
            udn=udn,
            upc=None,
            presentation_url=None,
            url='/description.xml',
            icons=[],
            xml=ET.Element('device'),
        )

        self._content_directory_cls = type(
            'BoundContentDirectoryService',
            (ContentDirectoryService,),
            {'_list_containers': staticmethod(list_containers), '_list_items': staticmethod(list_items)},
        )
        device_cls = type(
            'BoundMediaServerDevice',
            (UpnpServerDevice,),
            {
                'DEVICE_DEFINITION': device_info,
                'SERVICES': [self._content_directory_cls, ConnectionManagerService],
                'EMBEDDED_DEVICES': [],
            },
        )

        self._server = _Server(device_cls, source=source or ('0.0.0.0', 0), http_port=http_port or _free_tcp_port())

    async def start(self) -> None:
        await self._server.async_start()
        self.log.info('media server %s listening at %s', self._server.device.name, self._server.base_uri)

    async def stop(self) -> None:
        await self._server.async_stop()

    def on_files_changed(self) -> None:
        device = self._server.device
        if device is None:
            return
        service = device.services.get(self._content_directory_cls.SERVICE_DEFINITION.service_type)
        if isinstance(service, ContentDirectoryService):
            service.bump_update_id()
