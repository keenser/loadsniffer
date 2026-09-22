FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-libtorrent \
        python3-aiohttp \
        python3-aiofiles \
        youtube-dl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Not in Debian's repos; PEP 668 needs --break-system-packages on this base image.
# PyPI project name is "python-didl-lite" (imports as didl_lite); async-upnp-client
# already depends on it, listed here just to pin it explicitly.
RUN pip3 install --no-cache-dir --break-system-packages \
        async-upnp-client \
        python-didl-lite \
        uvloop \
        telnetlib3

WORKDIR /app
COPY . /app

VOLUME ["/var/lib/mrc"]

# mrc.py's own aiohttp server (8883), the telnet debug shell (8888) and UPnP/DLNA
# (SSDP multicast 1900/udp + dynamic ports for the MediaServer/GENA) - see compose
# file for why this only works with network_mode: host.
EXPOSE 8883/tcp 8888/tcp 1900/udp

ENTRYPOINT ["python3", "mrc.py"]
CMD ["/var/lib/mrc"]
