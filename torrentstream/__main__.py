#!/usr/bin/env python3
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#

import sys
import logging
import aiohttp.web
from . import torrentstream


def main():
    """ main loop """
    logging.basicConfig(level=logging.DEBUG)

    save_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/'
    ts = torrentstream.TorrentStream(save_path=save_path)

    app = aiohttp.web.Application()
    app.add_subapp('/bt/', ts.http)
    aiohttp.web.run_app(app, port=9999, reuse_port=True)


if __name__ == '__main__':
    main()
