#!/usr/bin/env python2.7
#
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
#
# Torrent Stream based on libtorrent

import libtorrent
from twisted.internet import reactor, threads
from twisted.web import server
from twisted.web.resource import Resource

class TorrentStream(Resource):
    def __init__(self, **options):
        self._torrent_handles = {}
        self._alert_handlers = {}
        session = libtorrent.session()
        self.session = session

        session.set_alert_mask(
                libtorrent.alert.category_t.tracker_notification |
                libtorrent.alert.category_t.stats_notification |
                libtorrent.alert.category_t.storage_notification |
                libtorrent.alert.category_t.progress_notification |
                libtorrent.alert.category_t.error_notification |
                libtorrent.alert.category_t.peer_notification
                )
        session.start_dht()
        session.start_lsd()
        session.start_upnp()
        session.start_natpmp()
        session.listen_on(options.get('min_port', 6881), options.get('max_port', 6889))
        reactor.callInThread(self._alert_queue_loop)

        session_settings = session.settings()
        session_settings.strict_end_game_mode = False
        session_settings.announce_to_all_tiers = True
        session_settings.announce_to_all_trackers = True
        session.set_settings(session_settings)

        session.add_dht_router("router.bittorrent.com", 6881)
        session.add_dht_router("router.utorrent.com", 6881)

        encryption_settings = libtorrent.pe_settings()
        encryption_settings.out_enc_policy = libtorrent.enc_policy(libtorrent.enc_policy.forced)
        encryption_settings.in_enc_policy = libtorrent.enc_policy(libtorrent.enc_policy.forced)
        encryption_settings.allowed_enc_level = libtorrent.enc_level.both
        encryption_settings.prefer_rc4 = True
        session.set_pe_settings(encryption_settings)

    def _alert_queue_loop(self):
        print("_alert_queue_loop")
        while reactor.running:
            if not self.session.wait_for_alert(1000):
                continue

            reactor.callLater(0, self._handle_alert, self.session.pop_alerts())

    def _handle_alert(self, alerts):
        for alert in alerts:
            print('{0}: {1}'.format(alert.what(), alert.message()))

    def add_torrent(self, url):
        add_torrent_params = {}
        add_torrent_params['url'] = url
        add_torrent_params['save_path'] = '/tmp/'
        add_torrent_params['storage_mode'] = libtorrent.storage_mode_t.storage_mode_sparse
        add_torrent_params['auto_managed'] = False
        self._torrent_handles[url] = self.session.add_torrent(add_torrent_params)
        self._torrent_handles[url].resume()

    def add_alert_handler(self, alert, handler):
        if handler not in self._alert_handlers.setdefault(alert,[]):
            self._alert_handlers[alert].append(handler)

    def remove_alert_handler(self, alert, handler):
        if alert in self._alert_handlers and handler in self._alert_handlers[alert]:
                self._alert_handlers[alert].remove(handler)

    def render_GET(self, request):
        if request.prepath[-1] == 'add' and request.args.get('url',[None])[0]:
            url = request.args.get('url')[0]
            self.add_torrent(url)

def main():
    root = Resource()
    torrentstream = TorrentStream()
    bt = Resource()
    bt.putChild("add", torrentstream)
    bt.putChild("info", torrentstream)
    root.putChild("bt", bt)
    site = server.Site(root)
    reactor.listenTCP(8880, site)

if __name__ == '__main__':
    reactor.callWhenRunning(main)
    reactor.run()
