//var mrcurl = "ws://nuc.grsk.eu.org:8883/ws";
function urlcfg(callback) {
    let urllocation = window.location.href;
    if ( urllocation.startsWith("chrome-extension") ) {
        chrome.storage.sync.get({
            mrcserver: 'ws://localhost:8883/ws'
        }, function(items) {
            callback(items.mrcserver);
        });
    }
    else {
        let mrcurl = urllocation.match(/:\/\/([^\/]+)/i);
        callback("ws://" + mrcurl[1] + "/ws");
    }
}

function MRCServer(url, handler) {
    let mrc = {};
    let doclose = false;
    let websocket = null ;
    let uid = 1;
    let callback_pool = {};
    mrc.url = url;
    mrc.disconnect = function() {
        doclose = true;
        console.log('websocket disconnect', websocket);
        websocket && websocket.close && websocket.close();
    }
    mrc.connect = function(opencallback, closecallback) {
        doclose = false;
        mrc.url(function(url) {
        console.log("url", url);
        websocket = new WebSocket(url);
        websocket.binaryType = "arraybuffer";
        websocket.onopen = opencallback;
        websocket.onclose = function(evt) {
            if (closecallback) {
                closecallback();
            }
            if (!doclose) {
                setTimeout(function() {
                    mrc.connect(opencallback, closecallback);
                }, 2000);
            }
        }
        websocket.onmessage = function(data) {
            console.debug('websocket <', data);
            let jsondata = JSON.parse(data.data);
            if (jsondata['_uid'] !== undefined) {
                callback_pool[jsondata['_uid']](jsondata);
                delete callback_pool[jsondata['_uid']];
            } else if (handler) {
                handler(jsondata);
            }
        }
        websocket.onerror = function(evt) {
            console.log('ws onerror: ', evt.type);
        }
        });
    }
    mrc.sendMessage = function(data, callback) {
        if (websocket && websocket.readyState == 1) {
            console.debug('websocket >', data);
            let senddata = data;
            if (callback !== undefined) {
                let currentid = uid++;
                senddata['_uid'] = currentid;
                callback_pool[currentid] = callback;
            }
            websocket.send(JSON.stringify(data));
        }
    }
    return mrc;
}
var mrc = new MRCServer(urlcfg, function(message) {
    console.log("ws message", message);
    if (message.action == 'btstatus') {
        UpdateBTStatus(message.response);
    } else if (message.action == 'upnpstatus') {
        UpdateUPNPStatus(message.response);
    }
});
var sendMessage = mrc.sendMessage;
var onStartupOrOnInstalledListener = function() {
    console.log("onStartupOrOnInstalledListener");
    mrc.connect(function() {
        console.log('mrc connected');
        mrc.sendMessage({
            action: 'btstatus',
        });
        mrc.sendMessage({
            action: 'upnpstatus',
        });
    },
    function() {
        console.log('mrc disconnected');
        UpdateUPNPStatus(null);
    });
}
var copyToClipboard = function (str) {
    document.oncopy = function(event) {
        event.clipboardData.setData('text/plain', str);
        event.preventDefault();
    }
    document.execCommand("Copy", false, null );
}
var addSingleLink = function(line, textcontent, url, title, cookie) {
    let span = document.createElement("span");
    span.textContent = textcontent;
    span.title = url;
    span.addEventListener('click', function(e) {
	if (sendMessage !== undefined) {
            sendMessage({
                action: "play",
                request: {
                    title: title,
                    cookie: cookie,
                    url: url
                }
            });
	}
        copyToClipboard(url);
    });
    line.appendChild(span);
}
var addLine = function(container, linkSource) {
    let line = document.createElement("div");
    let textcontent = linkSource.src + ': ' + linkSource.title || linkSource.url;
    addSingleLink(line, textcontent, linkSource.url, linkSource.title || linkSource.url, linkSource.cookie);
    if (linkSource.bitrate !== undefined) {
        for (let i = 0; i < linkSource.bitrate.length; i++) {
            addSingleLink(line, linkSource.bitrate[i].bitrate || i + 1, linkSource.bitrate[i].url, linkSource.title || linkSource.url, linkSource.bitrate[i].cookie);
        }
    }
    container.insertBefore(line, container.firstChild);
}
var addLinks = function(videoLinks) {
    let container = document.getElementById("content");
    for (let i = 0; i < videoLinks.length; ++i) {
        addLine(container, videoLinks[i]);
    }
}
var UpdateUPNPStatus = function(data) {
    let container = document.getElementById("upnp");
    let text = '';
    if (data) {
        text = data.device;
        if (data.state) {
            text = text + '[' + data.state + ']';
        }
        if (data.item.length > 0) {
            text = text + ": " + data.item[0].title;
        }
    }
    container.textContent = text;
}
var UpdateBTStatus = function(data) {
    let container = document.getElementById("bt");
    container.textContent = '';
    for (let i = 0; i < data.length; i++) {
        let title = document.createElement("span");
        title.textContent = data[i].title;
        let remove = document.createElement("span");
        remove.textContent = 'X';
        remove.title = 'Remove ' + data[i].title;
        remove.addEventListener('click',
        function(e) {
            if (sendMessage !== undefined) {
                sendMessage({
                    action: "rm",
                    request: {
                        url: data[i].info_hash
                    }
                });
            }
        });
        let head = document.createElement("div");
        head.appendChild(title);
        head.appendChild(remove);

        let files = document.createElement("div");
        for (let f = 0; f < data[i].files.length; f++) {
            addSingleLink(files, data[i].files[f].title, data[i].files[f].url, data[i].files[f].title, null);
        }
        files.className = 'active';
        title.addEventListener('click',
        function(e) {
            if (files.className === 'hided') {
                files.className = 'active';
            }
            else {
                files.className = 'hided';
            }
        });
        let torrent = document.createElement("div");
        torrent.className = 'torrent';
        torrent.appendChild(head);
        torrent.appendChild(files);
        container.appendChild(torrent);
    }
}
