from twisted.python import log
from twisted.web import http, proxy
from twisted.web.resource import Resource
from twisted.web import server
import urlparse

def printall(*args, **kwargs):
    print args, kwargs


class ProxyRequest(proxy.ProxyRequest):
    def process(self):
        print "uri", self.uri
        url = self.args.get('url',[None])[0]
        if not url:
            return
        cookies = self.args.get('cookie')
        parsed = urlparse.urlparse(url)

        protocol = parsed.scheme
        host = parsed.netloc
        port = self.ports[protocol]

        if cookies:
            self.requestHeaders.setRawHeaders(b"Cookie", cookies)
        self.requestHeaders.setRawHeaders(b"host", [host])
        if ':' in host:
            host, port = host.split(':')
            port = int(port)
        rest = urlparse.urlunparse(('', '') + parsed[2:])
        if not rest:
            rest = rest + '/'
        class_ = self.protocols[protocol]
        headers = self.getAllHeaders().copy()
        if 'host' not in headers:
            headers['host'] = host
        self.content.seek(0, 0)
        s = self.content.read()
        clientFactory = class_(self.method, rest, self.clientproto, headers,
                               s, self)
        self.reactor.connectTCP(host, port, clientFactory)
        d = self.notifyFinish()
        d.addCallback(lambda _: clientFactory.doStop())
        d.addErrback(lambda _: clientFactory.doStop())


class Proxy(proxy.Proxy):
    requestFactory = ProxyRequest


class ProxyFactory(http.HTTPFactory):
    protocol = Proxy

portstr = "tcp:8080:interface=0.0.0.0" # serve on localhost:8080

if __name__ == '__main__': # $ python proxy_modify_request.py
    import sys
    from twisted.internet import endpoints, reactor

    def shutdown(reason, reactor, stopping=[]):
        """Stop the reactor."""
        if stopping: return
        stopping.append(True)
        if reason:
            log.msg(reason.value)
        reactor.callWhenRunning(reactor.stop)

    log.startLogging(sys.stdout)
    endpoint = endpoints.serverFromString(reactor, portstr)
    d = endpoint.listen(ProxyFactory())
    d.addErrback(shutdown, reactor)
    reactor.run()
