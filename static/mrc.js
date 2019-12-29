function urlcfg(callback) {
    let urllocation = window.location;
    if ( urllocation.protocol == "chrome-extension:" ) {
        chrome.storage.sync.get({
            mrcserver: 'http://localhost:8883'
        }, function(items) {
            callback(new URL(items.mrcserver));
        });
    }
    else {
        callback(urllocation);
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
        wsurl = new URL('/ws', url);
        wsurl.protocol = 'ws:';
        console.log('wsurl', wsurl);
        websocket = new WebSocket(wsurl);
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

var fetchSimilarHeaders = function(url, callback) {
    let request = new XMLHttpRequest();
    request.onreadystatechange = function () {
        if (request.readyState === XMLHttpRequest.DONE) {
            //
            // The following headers may often be similar
            // to those of the original page request...
            //
            if (callback && typeof callback === 'function') {
                let headers = request.getAllResponseHeaders();
                let arr = headers.trim().split(/[\r\n]+/);
                let headerMap = {};
                arr.forEach(function (line) {
                    let parts = line.split(': ');
                    let header = parts.shift();
                    let value = parts.join(': ');
                    headerMap[header] = value;
                });

                callback(headerMap);
            }
        }
    };

    request.open('HEAD', url, true);
    request.send(null);
}

var vlcurl = function(url) {
    switch (navigator.platform) {
        case "iPad":
            return "vlc-x-callback://x-callback-url/stream?url=" + encodeURIComponent(url);
	default:
            return "vlc://" + url;
    }
}
var addSingleLink = function(line, textcontent, url, title, cookie, localurl) {
    let span = document.createElement("span");
    let relativeurl = url;
    let relative = false;

    if (localurl) {
        relative = true;
        relativeurl = new URL(url, localurl);
    }

    span.textContent = textcontent;
    span.title = relativeurl;
    span.addEventListener('click', function(e) {
	if (sendMessage !== undefined) {
            sendMessage({
                action: "transporturi",
                request: {
                    title: title,
                    cookie: cookie,
                    url: url,
                    relative: relative
                }
            });
	}
        copyToClipboard(relativeurl);
        let video = document.getElementById("video")
        if(video !== undefined && video.className === 'active') {
            fetchSimilarHeaders(url, function(headers) {
                video.setAttribute('src', relativeurl);
                video.setAttribute('type', headers['content-type']);
            });
        }
        let vlc = document.getElementById("vlcurl")
        if(vlc !== undefined) {
                vlccallback = vlcurl(relativeurl);
                vlc.setAttribute('href', vlccallback);
                vlc.textContent = textcontent;
                vlc.addEventListener('click',
                function(e) {
                    if(video !== undefined) {
                        video.pause();
                    }
                });
        }
    });
    line.appendChild(span);
}
var addLine = function(container, linkSource) {
    let line = document.createElement("div");
    let textcontent = linkSource.src + ': ' + linkSource.title || linkSource.url;
    addSingleLink(line, textcontent, linkSource.url, linkSource.title || linkSource.url, linkSource.cookie, null);
    if (linkSource.bitrate !== undefined) {
        for (let i = 0; i < linkSource.bitrate.length; i++) {
            addSingleLink(line, linkSource.bitrate[i].bitrate || i + 1, linkSource.bitrate[i].url, linkSource.title || linkSource.url, linkSource.bitrate[i].cookie, null);
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
    container.textContent = '';

    let refresh = document.createElement("span");
    refresh.textContent = '⟳';
    refresh.title = 'refresh';
    refresh.addEventListener('click',
    function(e) {
        if(sendMessage !== undefined) {
            sendMessage({
                action: "refresh"
            });
        }
    });

    container.appendChild(refresh);

    if (data) {
        let text = '';
        let stat = document.createElement("span");
        text = data.device;
        if (data.state) {
            text = text + '[' + data.state + ']';
        }
        if (data.item.length > 0) {
            text = text + ": " + data.item[0].title;
        }
        stat.textContent = text;

        let play = document.createElement("span");
        play.textContent = '►';
        play.title = 'play';
        play.addEventListener('click',
        function(e) {
            if(sendMessage !== undefined) {
                sendMessage({
                    action: "play"
                });
            }
        });

        let pause = document.createElement("span");
        pause.textContent = '❙❙';
        pause.title = 'pause';
        pause.addEventListener('click',
        function(e) {
            if(sendMessage !== undefined) {
                sendMessage({
                    action: "pause"
                });
            }
        });

        let stop = document.createElement("span");
        stop.textContent = '◼';
        stop.title = 'stop';
        stop.addEventListener('click',
        function(e) {
            if(sendMessage !== undefined) {
                sendMessage({
                    action: "stop"
                });
            }
        });
        container.appendChild(play);
        container.appendChild(pause);
        container.appendChild(stop);
        container.appendChild(stat);

        let video = document.getElementById("video")
        if(video !== undefined) {
            video.className = 'hided';
            video.pause()
        }
    }
    else {
        let video = document.getElementById("video")
        if(video !== undefined) {
            video.className = 'active';
        }
    }
}

var bthiddenlist = {}

var UpdateBTStatus = function(data) {
    urlcfg(function(localurl) {
    let hiddenlist = {}
    let container = document.getElementById("bt");
    container.textContent = '';
    for (let i = 0; i < data.length; i++) {
        let title = document.createElement("span");
        title.textContent = data[i].title;

        let recheck = document.createElement("span");
        recheck.textContent = '⟳';
        recheck.title = 'Recheck ' + data[i].title;
        recheck.addEventListener('click',
        function(e) {
            if (sendMessage !== undefined) {
                sendMessage({
                    action: "recheck",
                    request: {
                        url: data[i].info_hash
                    }
                });
            }
        });

        let remove = document.createElement("span");
        remove.textContent = '×';
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

        let load = document.createElement("span");
	load.textContent = '⇩';
        load.title = 'Download all files from ' + data[i].title;
        load.addEventListener('click',
        function(e) {
            if (sendMessage !== undefined) {
                sendMessage({
                    action: "load",
                    request: {
                        hash: data[i].info_hash
                    }
                });
            }
        });

        let head = document.createElement("div");
        head.appendChild(title);
        head.appendChild(load);
        head.appendChild(recheck);
        head.appendChild(remove);

        let files = document.createElement("div");
        for (let f = 0; f < data[i].files.length; f++) {
            addSingleLink(files, data[i].files[f].title, data[i].files[f].url, data[i].files[f].title, null, localurl);
        }
        if (bthiddenlist[data[i].info_hash] !== undefined) {
            files.className = bthiddenlist[data[i].info_hash];
            hiddenlist[data[i].info_hash] = bthiddenlist[data[i].info_hash];
        }
        else {
            files.className = 'hided';
            hiddenlist[data[i].info_hash] = 'hided';
        }
        title.addEventListener('click',
        function(e) {
            if (files.className === 'hided') {
                files.className = 'active';
                bthiddenlist[data[i].info_hash] = 'active';
            }
            else {
                files.className = 'hided';
                bthiddenlist[data[i].info_hash] = 'hided';
            }
        });
        let torrent = document.createElement("div");
        torrent.className = 'torrent';
        torrent.appendChild(head);
        torrent.appendChild(files);
        container.appendChild(torrent);
    }
    bthiddenlist = hiddenlist;
    });
}
