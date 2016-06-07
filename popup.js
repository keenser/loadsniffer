var tabid = null ;

var addSingleLink = function(line, textcontent, url, title) {
    var span = document.createElement("span");
    span.textContent = textcontent;
    span.title = url;
    span.addEventListener('click', function(e) {
        chrome.extension.sendMessage(
        {
            action: "play",
            play: {
                title: title,
                url: url
            }
        })
    });
    line.appendChild(span);
}

var addLine = function(container, linkSource) {
    console.log(linkSource);
    var line = document.createElement("div");
    addSingleLink(line, linkSource.src + ': ' + linkSource.title || linkSource.url, linkSource.url, linkSource.title || linkSource.url);
    if (typeof linkSource.bitrate !== 'undefined') {
        for (var i = 0; i < linkSource.bitrate.length; i++) {
            addSingleLink(line, linkSource.bitrate[i].bitrate || i + 1, linkSource.bitrate[i].url, linkSource.title || linkSource.url);
        }
    }
    //container.appendChild(line);
    container.insertBefore(line, container.firstChild);
}

var addLinks = function(videoLinks) {
    var container = document.getElementById("content");
    container.style.cursor = 'pointer';
    console.log('container', container);
    for (var i = 0; i < videoLinks.length; ++i) {
        addLine(container, videoLinks[i]);
    }
}

chrome.tabs.getSelected(null , function(tab) {
    tabid = tab.id;
    chrome.extension.sendMessage({
        action: "tabid",
        tabid: tabid
    }, function(urllib) {
        addLinks(urllib);
    });
});

chrome.extension.onMessage.addListener(function(request, sender) {
    if (request.action === 'addline') {
        var container = document.getElementById("content");
        addLine(container, request.addline);
    }
    else if (request.action === 'cleantab') {
        if (request.cleantab == tabid) {
            var container = document.getElementById("content");
            container.innerText = '';
        }
    }
    else if (request.action === 'upnp') {
        var container = document.getElementById("upnp");
        container.textContent = request.upnp.state + ": " + request.upnp.item[0].title;
    }
});

//function onWindowLoad() {
//  var message = document.querySelector('#message');
//
//chrome.tabs.getSelected(null,function(tab) {
//  console.log('tabs',tab);
//});
//  message.innerText = '11';
//  console.log('1:', _tabid);
//  chrome.extension.sendMessage(
//    { action: "getSource", tabid: _tabid},
//    function(backMessage){
//	console.log('2. popup onWindowLoad backMessage:', backMessage);
//        message.innerText = backMessage +" "+ _tabid;
//    }
// );
// console.log('3');
//}
//window.onload = onWindowLoad;
