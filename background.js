var urllib = [];
var tabid = null ;

function MRCServer(handler) {
    var mrc = {}
    doclose = false;
    websocket = null ;
    
    mrc.url = null ;
    mrc.disconnect = function() {
        doclose = true;
        console.log("disconnect", doclose);
        websocket && websocket.close && websocket.close();
    }
    ;
    
    mrc.connect = function() {
        console.log("connect", websocket);
        doclose = false;
        websocket = new WebSocket(mrc.url);
        websocket.binaryType = "arraybuffer";
        websocket.onclose = function(evt) {
            if (!doclose) {
                console.log("onclose", doclose);
                mrc.connect();
            }
        }
        ;
        websocket.onmessage = function(data) {
            var o = JSON.parse(data);
            handler(o);
        }
    }
    ;
    return mrc;
}
;

var mrc = new MRCServer(
function(obj) {
    console.log("onMessage", obj)
}
);
mrc.url = "ws://192.168.1.19:8881/ws";

chrome.extension.onMessage.addListener(function(request, sender, f_callback) {
    if (request.action == 'tabid') {
        console.log('request tabid info for', request);
        tabid = request.tabid;
        console.log('return tabId', urllib[request.tabid] || [])
        f_callback(urllib[request.tabid] || []);
    } else if (request.action == 'url') {
        console.log('request url', request.url);
    } else if (request.action == 'data') {
        console.log('data', request.data);
    }
});

var get = function(url, callback) {
    var xmlRequest = new XMLHttpRequest();
    xmlRequest.open('GET', url, true);
    xmlRequest.send();
    
    xmlRequest.onload = function() {
        callback(xmlRequest);
    }
    //    xmlRequest.onreadystatechange = function() {
    //        if (xmlRequest.readyState == 4) {
    //            callback(xmlRequest.responseXML);
    //        }
    //    }
    //    ;
}
;

var queryHeader = function(headers, headerName) {
    if (headers && headers.length) {
        for (var i = 0; i < headers.length; ++i) {
            var header = headers[i];
            if (header.name.toLowerCase() === headerName)
                return header.value;
        }
    }
    return '';
}
;


var LogListener = function(top, title, details, callback) {
    var type = queryHeader(details.responseHeaders, 'content-type');
    console.log("LogListener:", details.tabId, title, details.method, details.url, details.type, type, details.statusCode);
}
;

var CommonListener = function(top, title, details, callback) {
    var type = queryHeader(details.responseHeaders, 'content-type');
    console.log("CommonListener listener:", details.tabId, title, details.method, details.url, details.type, type, details.statusCode);
    console.log("CommonListener details:", details);
    var data = {
        src: 'common',
        url: details.url,
        title: title,
    };
    urllib[details.tabId].push(data);
    callback(data);
}
;

var RuTubeListener = function(top, title, details, callback) {
    var type = queryHeader(details.responseHeaders, 'content-type');
    console.log("Rutube listener:", details.tabId, title, details.method, details.url, details.type, type, details.statusCode);
    get(details.url, function(data) {
        var url = null ;
        try {
            url = data.responseXML.getElementsByTagName('m3u8')[0].textContent.trim();
        } 
        catch (e) {
            try {
                url = JSON.parse(data.responseText)['video_balancer']['m3u8'];
            } 
            catch (e) {
                return
            }
        }
        
        get(url, function(data) {
            bitrate = []
            var lines = data.responseText.split('\n');
            for (var i = 0; i < lines.length; i++) {
                var result = lines[i].match(/http:\/\/.*\.m3u8.*_(\d+)$/i)
                if (result) {
                    bitrate.push({
                        url: result[0],
                        bitrate: result[1]
                    });
                
                }
            }
            var data = {
                src: 'rutube',
                url: url,
                title: title,
                bitrate: bitrate
            };
            urllib[details.tabId].push(data);
            callback(data);
        
        });
    });
}
;

var HDSListener = function(top, title, details, callback) {
    var type = queryHeader(details.responseHeaders, 'content-type');
    console.log("TrackListener listener:", details.tabId, title, details.method, details.url, details.type, type, details.statusCode);
    url = details.url.substring(0, details.url.lastIndexOf('/'));
    for (var i = 0; i < urllib[details.tabId].length; i++) {
        if (urllib[details.tabId][i].url === url) {
            return;
        }
    }
    var data = {
        src: 'hds',
        url: url,
        title: title
    }
    urllib[details.tabId].push(data);
    callback(data);
}
;

var f4mListener = function(top, title, details, callback) {
    var type = queryHeader(details.responseHeaders, 'content-type');
    console.log("f4mListener:", details.tabId, details.method, title, details.url, details.type, type, details.statusCode);
    get(details.url, function(data) {
        xml = data.responseXML || null ;
        if (!xml) {
            return;
        }
        console.log('xml', xml);
        try {
            baseurl = xml.getElementsByTagName('baseURL')[0].textContent.trim();
        } 
        catch (e) {
            baseurl = "";
        }
        media = xml.getElementsByTagName('media');
        bitrate = []
        for (var i = 0; i < media.length; i++) {
            url = media[i].getAttribute("url") || baseurl + media[i].getAttribute("href")
            console.log(details.tabId, url, media[i].getAttribute("bitrate"));
            bitrate.push({
                url: url,
                bitrate: media[i].getAttribute("bitrate")
            })
        }
        var data = {
            src: 'f4m',
            url: details.url,
            title: title,
            bitrate: bitrate
        };
        urllib[details.tabId].push(data);
        callback(data);
    });
}
;

var onHeadersReceived = function(callback, urlfilter) {
    var removed = {};
    var onHeadersReceived = function(details) {
        var id = details.tabId;
        urllib[id] = urllib[id] || []
        if (id > -1 && !removed[id]) {
            chrome.tabs.get(id, function(tab) {
                callback(tab.url, tab.title, details, function(data) {
                    if (id == tabid) {
                        chrome.extension.sendMessage({
                            action: "addline",
                            addline: data
                        });
                    }
                });
            });
        }
        return null ;
    }
    ;
    chrome.tabs.onRemoved.addListener(function(tabId) {
        removed[tabId] = true
    });
    chrome.webRequest.onResponseStarted.addListener(onHeadersReceived, urlfilter, ["responseHeaders"]);
}
;

//onHeadersReceived(LogListener, {urls: ["<all_urls>"]});

onHeadersReceived(CommonListener, {
    urls: [
    "*://*/*.mp4*", 
    "*://*/*.flv*", 
    //"*://*/*video*",
    "*://*.youtube.com/embed/*", "*://*.youtube.com/watch?*", 
    ],
});

onHeadersReceived(RuTubeListener, {
    urls: ["*://*.rutube.ru/api/play/options/*", ],
});

onHeadersReceived(f4mListener, {
    urls: ["*://*/*.f4m*", ],
});

onHeadersReceived(HDSListener, {
    urls: ["*://*/*hds/track*"],
});

chrome.tabs.onUpdated.addListener(function(tabId, changeInfo, tab) {
    console.log('tabs.onUpdated', changeInfo, tab);
    if (changeInfo.status == 'loading'
    //&& typeof changeInfo.url === 'undefined'
    //&& typeof changeInfo.url !== 'undefined'
    ) {
        console.log('reload tabid:', tabId, changeInfo, tab);
        //        urllib[tabId] = [];
        //        chrome.extension.sendMessage({
        //            action: "cleantab",
        //            cleantab: tabId
        //        });    
    }
});

function context_onclick(info, tab) {
    console.log('context_onclick', info, tab)
    mrc.url = "ws://192.168.1.19:8880/ws";
}

chrome.contextMenus.create({
    title: "send to torrent2http",
    contexts: ['link', 'video'],
    onclick: context_onclick
});


var onStartupOrOnInstalledListener = function() {
    console.log("onStartupOrOnInstalledListener");
    mrc.connect();
}
;

chrome.runtime.onStartup.addListener(function() {
    onStartupOrOnInstalledListener();
});

chrome.runtime.onInstalled.addListener(function() {
    onStartupOrOnInstalledListener();
});
