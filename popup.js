var addSingleLink = function(line, linkSource) {
        var span = document.createElement("span");
        var a = document.createElement("a");
        a.textContent = linkSource.title || linkSource.url;
        a.setAttribute("href", linkSource.url);
        span.appendChild(a);
        line.appendChild(span);
        console.log(linkSource);
        if (typeof linkSource.bitrate !== 'undefined') {
            for (var i = 0; i < linkSource.bitrate.length; i++) {
                var span = document.createElement("span");
                var a = document.createElement("a");
                a.textContent = linkSource.bitrate[i].bitrate;
                a.setAttribute("href", linkSource.bitrate[i].url);
                a.setAttribute('download', linkSource.bitrate[i].url);
                span.appendChild(a);
                line.appendChild(span);
            }
        }
    };

var addLinks = function(videoLinks) {
        var container = document.getElementById("content");
        console.log('container', container);
        var general = document.createElement("div");
        general.setAttribute('class', 'content')
        for (var i = 0; i < videoLinks.length; ++i) {
            var line = document.createElement("div");
            addSingleLink(line, videoLinks[i]);
            general.appendChild(line);
        }
        container.replaceChild(general, container.childNodes[0]);
    };


chrome.tabs.getSelected(null, function(tab) {
    chrome.extension.sendMessage({
        action: "tabid",
        tabid: tab.id
    }, function(urllib) {
        addLinks(urllib);
    });
});

chrome.extension.onMessage.addListener(function(request, sender) {
    if (request.action == 'urllib') {
        addLinks(request.urllib);
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
