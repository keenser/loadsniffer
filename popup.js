var tabid = null ;
var addSingleLink = function(line, textcontent, url, title, cookie) {
    var span = document.createElement("span");
    span.textContent = textcontent;
    span.title = url;
    span.addEventListener('click', function(e) {
        chrome.extension.sendMessage({
            action: "play",
            play: {
                title: title,
                cookie: cookie,
                url: url
            }
        })
    });
    line.appendChild(span);
}
var addLine = function(container, linkSource) {
    var line = document.createElement("div");
    addSingleLink(line, linkSource.src + ': ' + linkSource.title || linkSource.url, linkSource.url, linkSource.title || linkSource.url, linkSource.cookie);
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
    for (var i = 0; i < btlib.length; ++i) {
        addLine(container, btlib[i]);
    }
}
chrome.tabs.getSelected(null , function(tab) {
    tabid = tab.id;
    chrome.extension.sendMessage({
        action: "tabid",
        tabid: tabid
    }, function(data) {
        addLinks(data.urllib);
        addBt(data.btlib);
    });
});
chrome.extension.onMessage.addListener(function(request, sender) {
    if (request.action === 'addline') {
        var container = document.getElementById("content");
        addLine(container, request.addline);
    } else if (request.action === 'cleantab') {
        if (request.cleantab == tabid) {
            var container = document.getElementById("content");
            container.innerText = '';
        }
    } else if (request.action === 'upnp') {
        var container = document.getElementById("upnp");
        container.textContent = request.upnp.state + ": " + request.upnp.item[0].title;
    } else if (request.action === 'btlib') {
        addBt(request.btlib);
    }
});