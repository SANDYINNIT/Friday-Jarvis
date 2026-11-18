(() => {
  'use strict';
  let started=false;
  function start() {
    if(started || !window.pywebview?.api)return;
    started=true;
    window.FieldCompanion.init({startTab:'lessons',
      call:(name,...args)=>window.pywebview.api[name](...args),
      close:()=>window.pywebview.api.hide_companion_surface()});
  }
  window.addEventListener('pywebviewready',start);
  start();
})();
