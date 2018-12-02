# loadsniffer
Chrome extension grab media resources from open tabs using mrc.py daemon as backend and chrome.webRequest api.
# mrc.py
Communicates with media renderers on local network using upnp-av protocol.
Find media resources from web pages using youtube-dl which can be played on media renderers.
Uses torrentstream.py module to stream media over torrent network.

# How to run
On server side(your home media server):

$ sudo apt-get install python3-libtorrent python3-xmltodict

$ sudo pip3 install youtube-dl aiohttp

$ ./mrc.py

On client side(your notebook):

goto http://yourserverip:8883/ (manage torrents only)

or add current project directory as chrome extencion for grab media resources from web pages. Don't forget to set mrc server Websocket URL in chrome extension settings

# torrentstream.py
Uses libtorrent and aiohttp to stream files inside torrents over http. Can be used as standalone server.

Example:

   $ torrentstream.py &

   $ curl localhost:9999/bt/add?url=http%3A%2F%2Fwww.frostclick.com%2Ftorrents%2Fvideo%2Fanimation%2FBig_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com.torrent

   {"status": "http://www.frostclick.com/torrents/video/animation/Big_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com.torrent added"}

   $ curl localhost:9999/bt/ls

   ["Big_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com/Big_Buck_Bunny_1080p_surround_FrostWire.com.avi", "Big_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com/PROMOTE_YOUR_CONTENT_ON_FROSTWIRE_01_06_09.txt", "Big_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com/Pressrelease_BickBuckBunny_premiere.pdf", "Big_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com/license.txt"]

   $ wget -O bbb.avi localhost:9999/bt/get?url=Big_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com%2FBig_Buck_Bunny_1080p_surround_FrostWire.com.avi

   $ curl localhost:9999/bt/rm?url=f84b51f0d2c3455ab5dabb6643b4340234cd036e
   
   {"status": "f84b51f0d2c3455ab5dabb6643b4340234cd036e removed"}
