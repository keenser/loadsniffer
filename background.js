var urllib = [];
var btlib = [];
var upnpstatus = null ;
var currenttabid = null ;
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
var get = function(url, callback) {
    var xmlRequest = new XMLHttpRequest();
    xmlRequest.open('GET', url, true);
    xmlRequest.send();
    xmlRequest.onload = function() {
        callback(xmlRequest);
    }
}
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
var LogListener = function(tabid, url, title, details, callback) {
    var type = queryHeader(details.responseHeaders, 'content-type');
    console.log("LogListener:", tabid, title, url, details.type, type);
}
var CommonListener = function(tabid, url, title, details, callback) {
    console.log("CommonListener:", tabid, title, url, details.type);
    url = url.replace(/Seg(\d)+-Frag(\d)+/, "");
    var data = {
        src: 'common',
        url: url,
        title: title,
    };
    callback(tabid, data);
}
var ResolveListener = function(tabid, url, title, details, callback) {
    console.log("ResolveListener:", tabid, title, url, details);
    mrc.sendMessage({
        action: 'search',
        search: {
            url: url
        }
    }, function(data) {
        callback(tabid, data);
    });
}
var RuTubeListener = function(tabid, url, title, details, callback) {
    console.log("RutubeListener:", tabid, title, url, details.type);
    get(url, function(data) {
        var m3u8;
        try {
            m3u8 = data.responseXML.getElementsByTagName('m3u8')[0].textContent.trim();
        } catch (e) {
            try {
                m3u8 = JSON.parse(data.responseText)['video_balancer']['m3u8'];
            } catch (e) {
                return
            }
        }
        get(m3u8, function(data) {
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
                src: '_rutube',
                url: m3u8,
                title: title,
                bitrate: bitrate
            };
            callback(tabid, data);
        });
    });
}
var HDSListener = function(tabid, url, title, details, callback) {
    console.log("TrackListener:", tabid, title, url, details.type);
    url = url.substring(0, url.lastIndexOf('/'));
    var data = {
        src: 'hds',
        url: url,
        title: title
    }
    callback(tabid, data);
}
var f4mListener = function(tabid, url, title, details, callback) {
    console.log("f4mListener:", tabid, title, url, details.type);
    get(url, function(data) {
        xml = data.responseXML || null ;
        if (!xml) {
            return;
        }
        console.log('xml', xml);
        try {
            baseurl = xml.getElementsByTagName('baseURL')[0].textContent.trim();
        } catch (e) {
            baseurl = "";
        }
        media = xml.getElementsByTagName('media');
        bitrate = []
        for (var i = 0; i < media.length; i++) {
            bitrateurl = media[i].getAttribute("url") || baseurl + media[i].getAttribute("href")
            bitrate.push({
                url: bitrateurl,
                bitrate: media[i].getAttribute("bitrate")
            })
        }
        var data = {
            src: 'f4m',
            url: url,
            title: title,
            bitrate: bitrate
        };
        callback(tabid, data);
    });
}
var MailRuListener = function(tabid, url, title, details, callback) {
    var type = queryHeader(details.responseHeaders, 'content-type');
    console.log("MailRuListener:", tabid, title, url, details.type, type);
    if (type === 'video/mp4') {
        chrome.cookies.get({
            url: url,
            name: 'video_key'
        }, function(cookie) {
            var data = {
                src: 'mailru',
                url: url,
                title: title,
                cookie: cookie.name + '=' + cookie.value
            };
            callback(tabid, data);
            return;
        });
    } else if (type.startsWith('application/json')) {
        jsonurl = url;
    } else {
        jsonurl = url;
        jsonurl = jsonurl.replace('https://my.mail.ru/', '');
        jsonurl = jsonurl.replace('/video/', '/');
        jsonurl = jsonurl.replace('/embed/', '/');
        jsonurl = jsonurl.replace('.html', '.json');
        jsonurl = jsonurl.replace('?', '.json');
        jsonurl = 'http://videoapi.my.mail.ru/videos/' + jsonurl;
    }
    get(jsonurl, function(data) {
        try {
            var jsondata = JSON.parse(data.responseText);
            chrome.cookies.get({
                url: jsonurl,
                name: 'video_key'
            }, function(cookie) {
                metaurl = jsondata['meta']['url'];
                title = jsondata['meta']['title'];
                bitrate = [];
                for (var i = 0; i < jsondata['videos'].length; i++) {
                    bitrate.push({
                        url: jsondata['videos'][i]['url'],
                        bitrate: jsondata['videos'][i]['key'],
                        cookie: cookie.name + '=' + cookie.value
                    })
                }
                var data = {
                    src: 'mailru',
                    url: metaurl,
                    title: title,
                    bitrate: bitrate
                };
                callback(tabid, data);
            });
        } catch (e) {
            console.log(e);
        }
    });
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
    chrome.extension.sendMessage({
        action: "btstatus",
        btstatus: btlib
    });
}
var UpdateUPNPStatus = function(data) {
    upnpstatus = data;
    console.log('UpdateUPNPStatus', data);
    chrome.extension.sendMessage({
        action: "upnpstatus",
        upnpstatus: data
    });
}
var UpdateTabLib = function(id, data) {
    urllib[id] = urllib[id] || []
    if (data !== null ) {
        console.log('UpdateTabLib', data);
        for (var i = 0; i < urllib[id].length; i++) {
            if (urllib[id][i].url === data.url) {
                return null ;
            }
        }
        urllib[id].push(data);
        // update popup if active:
        if (id == currenttabid) {
            chrome.extension.sendMessage({
                action: "addline",
                addline: data
            });
        }
    }
    if (urllib[id].length) {
        chrome.browserAction.setBadgeText({
            text: urllib[id].length.toString(),
            tabId: id
        });
    }
}
var onHeadersReceived = function(callback, urlfilter) {
    var removed = {};
    var onHeadersReceived = function(details) {
        var id = details.tabId;
        if (id > -1 && !removed[id]) {
            chrome.tabs.get(id, function(tab) {
                callback(id, details.url, tab.title, details, UpdateTabLib);
            });
        }
        return null ;
    }
    chrome.tabs.onRemoved.addListener(function(tabId) {
        removed[tabId] = true
    });
    chrome.webRequest.onResponseStarted.addListener(onHeadersReceived, urlfilter, ["responseHeaders"]);
}
//onHeadersReceived(LogListener, {urls: ["<all_urls>"]});
onHeadersReceived(CommonListener, {
    urls: ["*://*/*.mp4*", "*://*/*.flv*", "*://*/*.m3u8*", //"*://*/*video*",
    ],
});
onHeadersReceived(ResolveListener, {
    urls: ["*://*.youtube.com/embed/*", "*://*.youtube.com/watch?*", "*://rutube.ru/video/*", "*://rutube.ru/play/embed/*", "*://rutube.ru/tags/video/*", "*://rutube.ru/metainfo/tv/*", "*://*.vimeo.com/*/video/*", "*://*.vimeo.com/video/*", "*://*.vimeopro.com/*/video/*", "*://*.vimeopro.com/video/*", "*://*.vk.com/video*", ],
});
onHeadersReceived(RuTubeListener, {
    urls: ["*://*.rutube.ru/api/play/options/*", ],
});
onHeadersReceived(MailRuListener, {
    urls: ["*://*.my.mail.ru/*.mp4*", "*://*.mail.ru/*/video/*", "*://videoapi.my.mail.ru/videos/*"],
});
onHeadersReceived(f4mListener, {
    urls: ["*://*/*.f4m*", ],
});
onHeadersReceived(HDSListener, {
    urls: ["*://*/*hds/track*"],
});
function context_onclick(info, tab) {
    console.log('context_onclick', info, tab);
    mrc.sendMessage({
        action: 'add',
        add: {
            url: info.linkUrl
        }
    }, function(data) {
        UpdateTabLib(tab.id, data);
    });
}
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
        chrome.browserAction.setIcon({
            path: {
                "128": "icons/blue_128x128.png",
                "48": "icons/blue_48x48.png",
                "32": "icons/blue_32x32.png",
                "16": "icons/blue_16x16.png"
            }
        });
    },
    function() {
        console.log('mrc disconnected');
        UpdateUPNPStatus(null);
        chrome.browserAction.setIcon({
            path: {
                "128": "icons/grey_128x128.png",
                "48": "icons/grey_48x48.png",
                "32": "icons/grey_32x32.png",
                "16": "icons/grey_16x16.png"
            }
        });
    });
}
chrome.tabs.onUpdated.addListener(function(id, changeInfo, tab) {
    if (changeInfo.status == 'loading' && changeInfo.url !== undefined) {
        console.log('reload tabid:', id, changeInfo, tab);
        ResolveListener(id, tab.url, tab.title, tab, UpdateTabLib);
    }
});
chrome.extension.onMessage.addListener(function(request, sender, f_callback) {
    if (request.action == 'tabid') {
        currenttabid = request.tabid;
        f_callback({
            urllib: urllib[request.tabid] || [],
            btlib: btlib,
            upnpstatus: upnpstatus
        });
    } else if (request.action == 'play') {
        console.log('play', request.play);
        mrc.sendMessage(request);
    }
});
chrome.contextMenus.create({
    title: "send to loadsniffer",
    contexts: ['link', 'video'],
    onclick: context_onclick
});
chrome.browserAction.setBadgeBackgroundColor({
    color: '#2196F3'
});
chrome.runtime.onStartup.addListener(function() {
    onStartupOrOnInstalledListener();
});
chrome.runtime.onInstalled.addListener(function() {
    onStartupOrOnInstalledListener();
});
