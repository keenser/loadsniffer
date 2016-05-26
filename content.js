(function() {
var url = window.location.href;
console.log("url", url);

  chrome.extension.sendMessage(
    { action: "url", url: url}
 );

})();
