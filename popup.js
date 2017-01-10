var tabid = null ;
var sendMessage = chrome.extension.sendMessage;

chrome.tabs.getSelected(null , function(tab) {
    tabid = tab.id;
    sendMessage({
        action: "tabid",
        tabid: tabid
    }, function(data) {
        addLinks(data.urllib);
        addBt(data.btlib);
        UpdateUPNPStatus(data.upnpstatus);
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
    } else if (request.action === 'upnpstatus') {
        UpdateUPNPStatus(request.upnpstatus);
    } else if (request.action === 'btstatus') {
        addBt(request.btstatus);
    }
});
