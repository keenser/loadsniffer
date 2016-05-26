var urllib = [];
var tabid = null;

chrome.extension.onMessage.addListener(function(request, sender, f_callback) {
    if (request.action == 'tabid') {
        console.log('request tabid info for', request);
        tabid = request.tabid;
        console.log('return tabId', urllib[request.tabid] || [])
        f_callback(urllib[request.tabid] || []);
    } else if (request.action == 'url') {
        console.log('request url', request.url);
    }
});

var get = function(url, callback) {
        var xmlRequest = new XMLHttpRequest();
        xmlRequest.open('GET', url, true);
        xmlRequest.send();

        xmlRequest.onreadystatechange = function() {
            if (xmlRequest.readyState == 4) {
                callback(xmlRequest.responseXML);
            }
        };
    };

var queryHeader = function(headers, headerName) {
        if (headers && headers.length) {
            for (var i = 0; i < headers.length; ++i) {
                var header = headers[i];
                if (header.name.toLowerCase() === headerName) return header.value;
            }
        }
        return '';
    };

var CommonListener = function(top, title, details) {
        var type = queryHeader(details.responseHeaders, 'content-type');
        console.log("onResponseStarted listener:", details.tabId, title, details.method, details.url, details.type, type, details.statusCode);
        urllib[details.tabId].push({
            url: details.url,
            title: title,
        });
        return true;
    };

var TrackListener = function(top, title, details) {
        var type = queryHeader(details.responseHeaders, 'content-type');
        console.log("onResponseStarted listener:", details.tabId, title, details.method, details.url, details.type, type, details.statusCode);
        url = details.url.substring(0, details.url.lastIndexOf('/'));
        for (var i = 0; i < urllib[details.tabId].length; i++) {
            if (urllib[details.tabId][i].url === url) {
                return false;
            }
        }
        urllib[details.tabId].push({
            url: url,
            title: title,
            type: type
        });
        return true;
    };

var f4mListener = function(top, title, details) {
        var type = queryHeader(details.responseHeaders, 'content-type');
        console.log("f4mListener:", details.tabId, details.method, title, details.url, details.type, type, details.statusCode);
        get(details.url, function(xml) {
            console.log('xml', xml);
            media = xml.getElementsByTagName('media');
            bitrate = []
            for (var i = 0; i < media.length; i++) {
                console.log(details.tabId, media[i].getAttribute("url"), media[i].getAttribute("bitrate"));
                bitrate.push({
                    url: media[i].getAttribute("url"),
                    bitrate: media[i].getAttribute("bitrate")
                })
            }
            urllib[details.tabId].push({
                url: details.url,
                title: title,
                bitrate: bitrate
            });

        });
    };

var onHeadersReceived = function(callback, urlfilter) {
        var top = {},
            title = {},
            removed = {};
        var onHeadersReceived = function(details) {
                var id = details.tabId;
                urllib[id] = urllib[id] || []
                if (details.type == "main_frame") top[id] = details.url;
                if (id > -1 && !removed[id]) {
                    chrome.tabs.get(id, function(tab) {
                        if (chrome.runtime.lastError) {
                            //console.error(chrome.runtime.lastError.message);
                        } else if (tab) {
                            top[id] = tab.url;
                            title[id] = tab.title;
                        }
                        callback(top[id], title[id], details);
                        if (id == tabid) {
                            chrome.extension.sendMessage({
                                action: "urllib",
                                urllib: urllib[details.tabId]
                            });
                        }

                    });
                }
                return null;
            };
        chrome.tabs.onRemoved.addListener(function(tabId) {
            removed[tabId] = true
        });
        chrome.webRequest.onResponseStarted.addListener(onHeadersReceived, urlfilter, ["responseHeaders"]);
    };

onHeadersReceived(CommonListener, {
    urls: ["*://*/*.mp4*",
    //"*://*/*video*",
    "*://*.youtube.com/embed/*", "*://*.youtube.com/watch*",
    //   "<all_urls>"
    ],
});

onHeadersReceived(f4mListener, {
    urls: ["*://*/*.f4m*", ],
});

onHeadersReceived(TrackListener, {
    urls: ["*://*/*hds/track*"],
});

chrome.tabs.onUpdated.addListener(function(tabId, changeInfo, tab) {
    if (changeInfo.status == 'loading' && typeof changeInfo.url !== 'undefined') {
        console.log('reload tabid:', tabId, changeInfo);
        urllib[tabId] = [];
    }
});

var onStartupOrOnInstalledListener = function() {
        console.log("onStartupOrOnInstalledListener");
    };

chrome.runtime.onStartup.addListener(function() {
    onStartupOrOnInstalledListener();
});

chrome.runtime.onInstalled.addListener(function() {
    onStartupOrOnInstalledListener();
});
