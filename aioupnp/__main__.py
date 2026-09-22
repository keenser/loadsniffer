#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
"""Smoke test: advertise a MediaServer with a couple of fake files, browsable by any DLNA client."""

import asyncio
import logging
from . import MediaServer

FAKE_CONTAINERS = [{'id': 'demo', 'title': 'Demo torrent'}]
FAKE_ITEMS = {
    'demo': [
        {'id': '0', 'title': 'Big Buck Bunny.mp4', 'url': 'http://download.blender.org/peach/bigbuckbunny_movies/big_buck_bunny_480p_h264.mov', 'mime': 'video/mp4'},
    ],
}


def main():
    logging.basicConfig(level=logging.INFO)
    loop = asyncio.get_event_loop()

    server = MediaServer(
        list_containers=lambda: FAKE_CONTAINERS,
        list_items=lambda container_id: FAKE_ITEMS.get(container_id, []),
        friendly_name='loadsniffer (demo)',
    )

    loop.run_until_complete(server.start())
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(server.stop())


if __name__ == '__main__':
    main()
