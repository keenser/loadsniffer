var tabid = null;
var sendMessage = chrome.extension.sendMessage;
chrome.tabs.getSelected(null, function(tab) {
    tabid = tab.id;
    sendMessage({
        action: "tabid",
        tabid: tabid
    }, function(data) {
        addLinks(data.urllib);
        UpdateBTStatus(data.btlib);
        UpdateUPNPStatus(data.upnpstatus);
    });
});
chrome.extension.onMessage.addListener(function(message, sender) {
    if (message.action === 'addline') {
        let container = document.getElementById("content");
        addLine(container, message.response);
    } else if (message.action === 'cleantab') {
        if (message.cleantab == tabid) {
            let container = document.getElementById("content");
            container.innerText = '';
        }
    } else if (message.action === 'upnpstatus') {
        UpdateUPNPStatus(message.response);
    } else if (message.action === 'btstatus') {
        UpdateBTStatus(message.response);
    }
});
