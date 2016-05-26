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

var onResponseStartedListener = function(details) {
        if (details.tabId == -1) {
            return null;
        }
        urllib[details.tabId] = urllib[details.tabId] || []
        var type;
        for (var i = 0; i < details.responseHeaders.length; ++i) {
            if (details.responseHeaders[i].name.toLowerCase() === 'content-type') {
                type = details.responseHeaders[i].value;
                break;
            }
        }
        console.log("onResponseStarted listener: " + details.tabId + " " + details.method + " " + details.url + " " + details.type + " " + type + " -> " + details.statusCode);
            urllib[details.tabId].push( {
                url: details.url,
                bitrate: []
            });
        if (details.tabId == tabid) {
            chrome.extension.sendMessage({
                action: "urllib",
                urllib: urllib[details.tabId]
            });
        }
        return null;
    };

var f4mListener = function(details) {
        if (details.tabId == -1) {
            return null;
        }
        urllib[details.tabId] = urllib[details.tabId] || []
        var type;
        for (var i = 0; i < details.responseHeaders.length; ++i) {
            if (details.responseHeaders[i].name.toLowerCase() === 'content-type') {
                type = details.responseHeaders[i].value;
                break;
            }
        }
        console.log("f4mListener: " + details.tabId + " " + details.method + " " + details.url + " " + details.type + " " + type + " -> " + details.statusCode);
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
            urllib[details.tabId].push( {
                url: details.url,
                bitrate: bitrate
            });
            if (details.tabId == tabid) {
                chrome.extension.sendMessage({
                    action: "urllib",
                    urllib: urllib[details.tabId]
                });
            }

        });
        return null;
    }; // onResponseStartedListener
chrome.webRequest.onResponseStarted.addListener(onResponseStartedListener, {
    urls: [
    //"*://*/*track*",
    "*://*/*.mp4*",
    //"*://*/*video*",
    "*://*.youtube.com/embed/*", "*://*.youtube.com/watch*",
    //   "<all_urls>"
    ],
}, ["responseHeaders"]); // options: responseHeaders
chrome.webRequest.onResponseStarted.addListener(f4mListener, {
    urls: ["*://*/*.f4m*", ],
}, ["responseHeaders"]); // options: responseHeaders
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
