# loadsniffer
Chrome extension communicates with mrc.py over websocket to get media info from url, get and change status of upnp media renderers.
Also extension tries to find media resources on the page using chrome.webRequest 
# mrc.py
Communicates with media renderers on local network using coherence(prefer cohen fork) to send media to them using upnp-av.
Find media resources from web pages using youtube-dl which can be sended to media renderers.
Uses torrentstream module to get access to media resources inside torrents.
Uses websocket(autobahn) to communicate with chrome extension

On server side(your home media server):

$ sudo apt-get install python-libtorrent python-autobahn python-livestreamer

$ sudo pip install youtube-dl Cohen

$ ./mrc.py &

On client side(your notebook):

goto http://serverip:8882 (manage torrents only)

or add current project directory as chrome extencion for grab media resources from web pages 

# torrentstream.py
Uses libtorrent and twisted.web to stream files inside torrents over http. Can be used as standalone server.

Example:

   $ torrentstream.py &

   $ curl localhost:8882/bt/add?url=http%3A%2F%2Fwww.frostclick.com%2Ftorrents%2Fvideo%2Fanimation%2FBig_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com.torrent

   {"status": "http://www.frostclick.com/torrents/video/animation/Big_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com.torrent added"}

   $ curl localhost:8882/bt/ls

   ["Big_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com/Big_Buck_Bunny_1080p_surround_FrostWire.com.avi", "Big_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com/PROMOTE_YOUR_CONTENT_ON_FROSTWIRE_01_06_09.txt", "Big_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com/Pressrelease_BickBuckBunny_premiere.pdf", "Big_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com/license.txt"]

   $ wget -O bbb.avi localhost:8882/bt/get?url=Big_Buck_Bunny_1080p_surround_frostclick.com_frostwire.com%2FBig_Buck_Bunny_1080p_surround_FrostWire.com.avi

   $ curl localhost:8882/bt/rm?url=f84b51f0d2c3455ab5dabb6643b4340234cd036e
   
   {"status": "f84b51f0d2c3455ab5dabb6643b4340234cd036e removed"}
