chrome.tabs.getSelected(null,function(tab) {
  console.log('tabs',tab);
  chrome.extension.sendMessage(
    { action: "tabid", tabid: tab.id},
    function(urllib){
        console.log('tabs.getSelected.backMessage:', urllib);
        message.innerText = urllib;
    }
 );

});

chrome.extension.onMessage.addListener(function(request, sender) {
  if ( request.action == 'urllib') {
  console.log('popup onMessage',request);
  var message = document.querySelector('#message');
  message.innerText = request.urllib;
 }
});

//function onWindowLoad() {
//
//chrome.tabs.getSelected(null,function(tab) {
//  console.log('tabs',tab);
//});

//  var message = document.querySelector('#message');
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
