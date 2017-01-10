var mrcurl = "ws://nuc.grsk.eu.org:8881"
function MRCServer(url, handler) {
    var mrc = {};
    var doclose = false;
    var websocket = null ;
    var uid = 1;
    var callback_pool = {};
    mrc.url = url;
    mrc.disconnect = function() {
        doclose = true;
        console.log("disconnect", websocket);
        websocket && websocket.close && websocket.close();
    }
    mrc.connect = function(opencallback, closecallback) {
        doclose = false;
        websocket = new WebSocket(mrc.url);
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
            console.log("onmessage websocket", data);
            var jsondata = JSON.parse(data.data);
            if (jsondata['_uid'] !== undefined) {
                callback_pool[jsondata['_uid']](jsondata['data']);
                delete callback_pool[jsondata['_uid']];
            } else if (handler) {
                handler(jsondata);
            }
        }
    }
    mrc.sendMessage = function(data, callback) {
        console.log('send', data);
        if (websocket.readyState) {
            var senddata = data;
            if (callback !== undefined) {
                var currentid = uid++;
                senddata['_uid'] = currentid;
                callback_pool[currentid] = callback;
            }
            websocket.send(JSON.stringify(data));
        }
    }
    return mrc;
}
var mrc = new MRCServer(mrcurl,function(request) {
    console.log("ws request", request);
    if (request.action == 'btupdate') {
        UpdateBTStatus(request.btupdate);
    } else if (request.action == 'upnpupdate') {
        UpdateUPNPStatus(request.upnpupdate);
    }
});
var sendMessage = mrc.sendMessage;
var onStartupOrOnInstalledListener = function() {
    console.log("onStartupOrOnInstalledListener");
    mrc.connect(function() {
        console.log('mrc connected');
        mrc.sendMessage({
            action: 'btstatus',
        }, UpdateBTStatus);
        mrc.sendMessage({
            action: 'upnpstatus',
        }, UpdateUPNPStatus);
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
    var span = document.createElement("span");
    span.textContent = textcontent;
    span.title = url;
    span.addEventListener('click', function(e) {
	if (sendMessage !== undefined) {
            sendMessage({
                action: "play",
                play: {
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
    var line = document.createElement("div");
    if (linkSource.src == 'bt') {
        var textcontent = linkSource.title || linkSource.url;
    }
    else {
        var textcontent = linkSource.src + ': ' + linkSource.title || linkSource.url;
    }
    addSingleLink(line, textcontent, linkSource.url, linkSource.title || linkSource.url, linkSource.cookie);
    if (linkSource.bitrate !== undefined) {
        for (var i = 0; i < linkSource.bitrate.length; i++) {
            addSingleLink(line, linkSource.bitrate[i].bitrate || i + 1, linkSource.bitrate[i].url, linkSource.title || linkSource.url, linkSource.bitrate[i].cookie);
        }
    }
    //container.appendChild(line);
    container.insertBefore(line, container.firstChild);
}
var addLinks = function(videoLinks) {
    var container = document.getElementById("content");
    container.style.cursor = 'pointer';
    for (var i = 0; i < videoLinks.length; ++i) {
        addLine(container, videoLinks[i]);
    }
}
var addBt = function(btlib) {
    var container = document.getElementById("bt");
    container.style.cursor = 'pointer';
    container.textContent = '';
    for (var i = btlib.length - 1; i >= 0; --i) {
        addLine(container, btlib[i]);
    }
}
var UpdateUPNPStatus = function(data) {
    var container = document.getElementById("upnp");
    var text = '';
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
    console.log('UpdateBTStatus', data);
    btlib = []
    for (var i = 0; i < data.files.length; i++) {
        btlib.push({
            src: 'bt',
            url: data.prefix + encodeURIComponent(data.files[i]),
            title: data.files[i]
        });
    }
    addBt(btlib);
}