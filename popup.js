var addSingleLink = function(tr, index, linkSource) {
        var td = document.createElement("td");
        td.setAttribute('rule', 'url');
        var a = document.createElement("a");
        a.textContent = linkSource.title || linkSource.url;
        a.setAttribute("href", linkSource.url);
        td.appendChild(a);
        tr.appendChild(td);
        console.log(linkSource);
        if (typeof linkSource.bitrate !== 'undefined') {
            for (var i = 0; i < linkSource.bitrate.length; i++) {
                var td = document.createElement("td");
                td.setAttribute('rule', 'bitrate');
                var a = document.createElement("a");
                a.textContent = linkSource.bitrate[i].bitrate;
                a.setAttribute("href", linkSource.bitrate[i].url);
                a.setAttribute('download', linkSource.bitrate[i].url);
                td.appendChild(a);
                tr.appendChild(td);
            }
        }
    };

var addLinks = function(videoLinks) {
        var container = document.getElementById("content");
        console.log('container', container);
        var table = document.createElement("table");
        for (var i = 0; i < videoLinks.length; ++i) {
            var tr = document.createElement("tr");
            tr.setAttribute('index', i);
            addSingleLink(tr, i, videoLinks[i]);
            table.appendChild(tr);
        }
        container.replaceChild(table, container.childNodes[0]);
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
