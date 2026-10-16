function getHTTPObject() {
    var xmlhttp = false;
    if (typeof XMLHttpRequest != 'undefined') {
            try {
                    xmlhttp = new XMLHttpRequest();
            } catch (e) {
                    xmlhttp = false;
            }
    } else {
    /*@cc_on
    @if (@_jscript_version >= 5)
        try {
            xmlhttp = new ActiveXObject("Msxml2.XMLHTTP");
        } catch (e) {
            try {
                xmlhttp = new ActiveXObject("Microsoft.XMLHTTP");
            } catch (E) {
                xmlhttp = false;
            }
        }
    @end @*/
}
    return xmlhttp;
}

function login()
{
var username = document.getElementById("basic-username").value;
var password = document.getElementById("basic-password").value;
var path = window.location.pathname.split("/");
var basepath = path.slice(0, path.length-1).join("/");
var http = getHTTPObject();
    //var url = "http://" + username + ":" + password + "@" + this.action.substr(7);
    var url = basepath + "/";
http.open("get", url, false, username, password);
http.send("");
    if (http.status == 200) {
        document.location = url;
    } else if (http.status == 429) {
        var minutes = Math.ceil(parseInt(http.getResponseHeader("Retry-After"), 10) / 60) || 1;
        alert("Too many login attempts! Please try again in " + minutes + (minutes == 1 ? " minute." : " minutes."));
    } else {
        alert("Incorrect username and/or password!");
    }
return false;
}

document.onkeypress = enter;
function enter(e) {
if (e.which == 13) { login(); }
}