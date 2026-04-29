
  (function(){
    var bc = document.body.className || '';
    var m = bc.match(/view-([a-z]+)/);
    var v = m ? m[1] : 'all';
    document.querySelectorAll('#view-nav-strip a[data-view]').forEach(function(a){
      if (a.dataset.view === v) {
        a.style.background = '#1e2a42';
        a.style.color = '#00d4ff';
        a.style.fontWeight = 'bold';
      }
    });
  })();
