$("#menu-toggle").click(function(e) {
e.preventDefault();
$("#wrapper").toggleClass("toggled");
});

function makePostRequest(url, data, callback) {
$.ajax({
  type: 'POST',
  url: url,
  data: data,
  contentType: "application/json",
  success: function (result) {
      if(typeof callback == 'function') {
        clearTimeout(timer);
        callback.call();
      }
  }
});
}

$(function(){
$("a[post=true]").each(function () {
    $(this).on('click', function () {
        makePostRequest(
            $(this).attr('phref'),
            $(this).attr('pdata'),
            appstatus
        );
    });
});
});

$(function(){
$("input[post=true]").each(function () {
    $(this).on('change', function () {
      var data = JSON.parse($(this).attr('pdata'));
      data.function = $(this).is(':checked') ? 'enable' : 'disable';
      data =  JSON.stringify(data)
        makePostRequest(
            $(this).attr('phref'),
            data,
            appstatus
        );
    });
});
});

// The layouts of the dashboard, in the account menu (templates/macros.html, see the README): a click on another layout
// saves it for this account, then the page loads again with it. JSON only, as for the services above. The menu stays
// open meanwhile: when the layout cannot be saved, it says so there, and the page stays as it is.
$(function(){
$("button[data-layout]").each(function () {
    var item = $(this);
    item.on('click', function (e) {
        e.stopPropagation();  // Bootstrap would close the menu
        if (item.attr('aria-current') === 'true') {
            return;  // the layout shown
        }
        $.ajax({
            type: 'POST',
            url: item.attr('data-url'),
            data: JSON.stringify({layout: item.attr('data-layout')}),
            contentType: "application/json",
            success: function () { window.location.reload(); },
            error: function () { $("#layout-error").removeAttr('hidden'); }
        });
    });
});
});
