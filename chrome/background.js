var urllib = []
var btlib = []
var upnpstatus = null 
var currenttabid = null 
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
        for (let i = 0; i < headers.length; ++i) {
            let header = headers[i];
            if (header.name.toLowerCase() === headerName)
                return header.value;
        }
    }
    return '';
}
var LogListener = function(tabid, url, title, details, callback) {
    let type = queryHeader(details.responseHeaders, 'content-type');
    console.log("LogListener:", tabid, title, url, details.type, type);
}
var CommonListener = function(tabid, url, title, details, callback) {
    console.log("CommonListener:", tabid, title, url, details.type);
    url = url.replace(/Seg(\d)+-Frag(\d)+/, "");
    let ret = {
        src: 'common',
        url: url,
        title: title,
    };
    callback(tabid, ret);
}
var ResolveListener = function(tabid, url, title, details, callback) {
    console.log("ResolveListener:", tabid, title, url, details);
    mrc.sendMessage({
        action: 'search',
        request: {
            url: url
        }
    }, function(data) {
        callback(tabid, data.response);
    });
}
var RuTubeListener = function(tabid, url, title, details, callback) {
    console.log("RutubeListener:", tabid, title, url, details.type);
    get(url, function(data) {
        let m3u8;
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
            let lines = data.responseText.split('\n');
            for (let i = 0; i < lines.length; i++) {
                let result = lines[i].match(/http:\/\/.*\.m3u8.*_(\d+)$/i)
                if (result) {
                    bitrate.push({
                        url: result[0],
                        bitrate: result[1]
                    });
                }
            }
            let ret = {
                src: '_rutube',
                url: m3u8,
                title: title,
                bitrate: bitrate
            };
            callback(tabid, ret);
        });
    });
}
var HDSListener = function(tabid, url, title, details, callback) {
    console.log("TrackListener:", tabid, title, url, details.type);
    url = url.substring(0, url.lastIndexOf('/'));
    let ret = {
        src: 'hds',
        url: url,
        title: title
    }
    callback(tabid, ret);
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
        for (let i = 0; i < media.length; i++) {
            bitrateurl = media[i].getAttribute("url") || baseurl + media[i].getAttribute("href")
            bitrate.push({
                url: bitrateurl,
                bitrate: media[i].getAttribute("bitrate")
            })
        }
        let ret = {
            src: 'f4m',
            url: url,
            title: title,
            bitrate: bitrate
        };
        callback(tabid, ret);
    });
}
var MailRuListener = function(tabid, url, title, details, callback) {
    let type = queryHeader(details.responseHeaders, 'content-type');
    console.log("MailRuListener:", tabid, title, url, details.type, type);
    if (type === 'video/mp4') {
        chrome.cookies.get({
            url: url,
            name: 'video_key'
        }, function(cookie) {
            let ret = {
                src: 'mailru',
                url: url,
                title: title,
                cookie: cookie.name + '=' + cookie.value
            };
            callback(tabid, ret);
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
            let jsondata = JSON.parse(data.responseText);
            chrome.cookies.get({
                url: jsonurl,
                name: 'video_key'
            }, function(cookie) {
                metaurl = jsondata['meta']['url'];
                title = jsondata['meta']['title'];
                bitrate = [];
                for (let i = 0; i < jsondata['videos'].length; i++) {
                    bitrate.push({
                        url: jsondata['videos'][i]['url'],
                        bitrate: jsondata['videos'][i]['key'],
                        cookie: cookie.name + '=' + cookie.value
                    })
                }
                let ret = {
                    src: 'mailru',
                    url: metaurl,
                    title: title,
                    bitrate: bitrate
                };
                callback(tabid, ret);
            });
        } catch (e) {
            console.log(e);
        }
    });
}
var UpdateBTStatus = function(data) {
    btlib = data;
    console.log('UpdateBTStatus', data);
    chrome.extension.sendMessage({
        action: 'btstatus',
        response: btlib
    });
}
var UpdateUPNPStatus = function(data) {
    upnpstatus = data;
    console.log('UpdateUPNPStatus', data);
    chrome.extension.sendMessage({
        action: 'upnpstatus',
        response: upnpstatus
    });
}
var UpdateTabLib = function(id, data) {
    urllib[id] = urllib[id] || []
    if (typeof(data) === 'object' && data) {
        console.log('UpdateTabLib', data);
        for (let i = 0; i < urllib[id].length; i++) {
            if (urllib[id][i].url === data.url) {
                return null ;
            }
        }
        urllib[id].push(data);
        // update popup if active:
        if (id == currenttabid) {
            chrome.extension.sendMessage({
                action: "addline",
                response: data
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
    let removed = {};
    let onHeadersReceived = function(details) {
        let id = details.tabId;
        if (id > -1 && !removed[id]) {
            chrome.tabs.get(id, function(tab) {
                callback(id, details.url, tab.title, details, UpdateTabLib);
            });
        }
        return null ;
    }
    chrome.tabs.onRemoved.addListener(function(tabId) {
        console.log('remove tab', tabId);
        removed[tabId] = true;
        delete urllib[tabId];
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
        request: {
            url: info.linkUrl
        }
    }, function(data) {
        if (data.response !== 'done') {
            UpdateTabLib(tab.id, data.response);
        }
    });
}
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
        chrome.browserAction.setIcon({
            path: {
                "128": "chrome/icons/blue_128x128.png",
                "48": "chrome/icons/blue_48x48.png",
                "32": "chrome/icons/blue_32x32.png",
                "16": "chrome/icons/blue_16x16.png"
            }
        });
    }, function() {
        console.log('mrc disconnected');
        UpdateUPNPStatus(null );
        chrome.browserAction.setIcon({
            path: {
                "128": "chrome/icons/grey_128x128.png",
                "48": "chrome/icons/grey_48x48.png",
                "32": "chrome/icons/grey_32x32.png",
                "16": "chrome/icons/grey_16x16.png"
            }
        });
    });
}
chrome.tabs.onUpdated.addListener(function(id, changeInfo, tab) {
    if (changeInfo.status == 'loading' && changeInfo.url !== undefined && tab.url.startsWith('http')) {
        console.debug('reload tabid:', id, changeInfo, tab);
        ResolveListener(id, tab.url, tab.title, tab, UpdateTabLib);
    }
});
chrome.extension.onMessage.addListener(function(message, sender, f_callback) {
    if (message.action == 'tabid') {
        currenttabid = message.tabid;
        f_callback({
            urllib: urllib[message.tabid] || [],
            btlib: btlib,
            upnpstatus: upnpstatus
        });
    } else if (message.action !== undefined) {
        console.log(message.action, message);
        mrc.sendMessage(message);
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
//chrome.runtime.onStartup.addListener(function() {
//    onStartupOrOnInstalledListener();
//});
//chrome.runtime.onInstalled.addListener(function() {
//    onStartupOrOnInstalledListener();
//});
document.onload = onStartupOrOnInstalledListener();
