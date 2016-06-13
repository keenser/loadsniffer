# loadsniffer
Chrome extension to parce media resources on the page using chrome.webRequest or youtube_dl.
Send it to media renderer using upnp-av.

Extension communicates with mrc.py over websocket to resolve urls get info from media renderer and send media to it over upnp.
mrc.py use coherence(prefer cohen fork) to communicate with media renderers.
