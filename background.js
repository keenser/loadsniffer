var verboseLevel = 2; // 0: nothing, 1: extension init and errors, 2: every request, nicely formatted, 3: lots of details
var urllib = [];
var tabid = null;
//var onBeforeRequestListener = function(details) {
//  console.log("    in onBeforeRequestListener listener: "+EXACT(details.method)+" "+EXACT(details.url)+" "+EXACT(details.type)+" -> "+details.statusCode);
//  return null;
//}

chrome.extension.onMessage.addListener(function(request, sender, f_callback){
  if (request.action == 'tabid') {
	console.log('request tabid info for', request);
	tabid = request.tabid;
	f_callback(urllib[request.tabid]); //обратное сообщение
 }
 else if (request.action == 'url') {
	console.log('request url', request.url);
 }
});

var get=function(url,callback){
  var xmlRequest=new XMLHttpRequest();
  xmlRequest.open('GET',url,true);
  //xmlRequest.overrideMimeType('text/xml');
  xmlRequest.send();

  xmlRequest.onreadystatechange=function(){
    console.log("get", xmlRequest);
    if(xmlRequest.readyState==4){
      callback(xmlRequest.responseXML);
    }
  };
};

var onResponseStartedListener = function(details) {
  if (details.tabId == -1) {
    return null;
  }
  var type = details.type;
//  if (type !== "other" && type !== "object") {
//    return null;
//  }
  for (var i = 0; i < details.responseHeaders.length; ++i) {
    if (details.responseHeaders[i].name.toLowerCase() === 'content-type') {
      type = details.responseHeaders[i].value;
      break;
    }
  }
  console.log("onResponseStarted listener: "+details.tabId+" "+details.method+" "+details.url+" "+details.type+" "+type+" -> "+details.statusCode);
  get(details.url,function(xml) {
     console.log('xml', xml);
  });
  (urllib[details.tabId] = urllib[details.tabId] || []).push(details.url);
  if ( details.tabId == tabid) {
  chrome.extension.sendMessage(
    { action: "urllib", urllib: urllib[details.tabId]}
 );
  }
  return null;
};  // onResponseStartedListener

chrome.webRequest.onResponseStarted.addListener(onResponseStartedListener,
  {
    urls:[
//"*://*/*track*",
"*://*/*.mp4*",
//"*://*/*video*",
"*://*/*.f4m*",
"*://*.youtube.com/embed/*",
"*://*.youtube.com/watch*",
//   "<all_urls>"
    ],
  }
, ["responseHeaders"]);  // options: responseHeaders

chrome.tabs.onUpdated.addListener(function(tabId,changeInfo,tab){
   if (changeInfo.status == 'loading') {
     console.log('reload tabid:', tabId, changeInfo);
     urllib[tabId] = [];
   }
});

var onStartupOrOnInstalledListener = function() {
};  // onStartupOrOnInstalledListener
chrome.runtime.onStartup.addListener(function() {
  if (verboseLevel >= 1) console.log("        in onStartup listener");
  onStartupOrOnInstalledListener();
  if (verboseLevel >= 1) console.log("        out onStartup listener");
});
chrome.runtime.onInstalled.addListener(function() {
  if (verboseLevel >= 1) console.log("        in onInstalled listener");
  onStartupOrOnInstalledListener();
  if (verboseLevel >= 1) console.log("        out onInstalled listener");
});  // onInstalled listener
