/* Finite, cancellable presentation; never a readiness or provider signal. */
(() => {
  'use strict';
  const surface=document.getElementById('friday-entrance');
  if(!surface)return;
  let generation=0, animations=[], timer, plays=0;
  let ready=document.readyState!=='loading', pending, epoch, observed=0, nativeVisible=false;
  let reason='Waiting for the window';
  const retiredEpochs=new Set();
  function motionReason() {
    try {
      if(matchMedia('(prefers-reduced-motion: reduce)').matches ||
        localStorage.getItem('friday.ui.reducedMotion')==='1')return 'Reduced motion is enabled';
      if(localStorage.getItem('friday.ui.lowPower')==='1')return 'Low-power rendering is enabled';
      return '';
    }catch(_){return 'Motion preferences are unavailable';}
  }
  function dismiss() {
    generation++;clearTimeout(timer);
    for(const a of animations)a.cancel();
    animations=[];surface.hidden=true;
  }
  function play() {
    dismiss();
    reason=motionReason();
    if(document.hidden){reason='Waiting for the window';return false;}
    if(reason)return false;
    if(typeof surface.animate!=='function'){reason='Opening animation is unavailable';return false;}
    const ticket=generation;
    surface.hidden=false;plays++;reason='Playing';
    const animate=(target,frames,options)=>{
      const a=target.animate(frames,{fill:'both',easing:'cubic-bezier(.2,.8,.2,1)',...options});
      animations.push(a);return a;
    };
    try {
      animate(surface,[{opacity:1,offset:0},{opacity:1,offset:.78},{opacity:0,offset:1}],{duration:1600,easing:'linear'});
      animate(surface.querySelector('.entrance-paper'),[{transform:'scale(.78) rotate(-6deg)',opacity:0},{transform:'scale(1) rotate(0)',opacity:1}],{duration:680});
      animate(surface.querySelector('.entrance-mark'),[{transform:'translateY(24px) scale(.5)',opacity:0},{transform:'translateY(-3px) scale(1.04)',opacity:1,offset:.75},{transform:'translateY(0) scale(1)',opacity:1}],{duration:680});
      animate(surface.querySelector('.entrance-stroke'),[{transform:'scaleX(0)'},{transform:'scaleX(1)'}],{duration:650});
      animate(surface.querySelector('.entrance-wordmark'),[{opacity:0,transform:'translateY(8px)',letterSpacing:'.3em'},{opacity:1,transform:'translateY(0)',letterSpacing:'.14em'}],{duration:550,delay:180});
      animate(surface.querySelector('.entrance-caption'),[{opacity:0},{opacity:1}],{duration:350,delay:340});
      Promise.allSettled(animations.map(a=>a.finished)).then(()=>{if(ticket===generation){dismiss();reason='Complete';}});
      timer=setTimeout(()=>{if(ticket===generation){dismiss();reason='Complete';}},2000);
      return true;
    }catch(_){dismiss();reason='Opening animation is unavailable';return false;}
  }
  function revealPending() {
    if(!pending || !ready || !nativeVisible || document.hidden)return;
    pending=null;play();
  }
  window.FieldEntrance={play,dismiss,observe(state){
    // Numeric input is retained for old presentation readers. The shipped
    // bridge supplies the exact window epoch plus native visibility/readiness.
    if(typeof state==='number' && epoch && epoch!=='legacy')return;
    const value=typeof state==='number'?{epoch:'legacy',revision:state,ready:true,visible:true}:state?.opening;
    if(!value || typeof value.epoch!=='string' || !value.epoch ||
      !Number.isSafeInteger(value.revision) || value.revision<0 || retiredEpochs.has(value.epoch))return;
    if(epoch!==value.epoch){
      if(epoch)retiredEpochs.add(epoch);
      if(retiredEpochs.size>16)retiredEpochs.delete(retiredEpochs.values().next().value);
      epoch=value.epoch;observed=0;pending=null;dismiss();
    }
    if(value.revision<observed)return;
    nativeVisible=value.ready===true && value.visible===true;
    if(value.revision>observed){observed=value.revision;pending={epoch,revision:observed};}
    if(!nativeVisible){dismiss();reason='Waiting for the window';return;}
    revealPending();
  },snapshot:()=>({active:!surface.hidden,plays,reason,epoch,revision:observed,pending:!!pending})};
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){pending=null;dismiss();reason='Dismissed';}});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)dismiss();else revealPending();});
  matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change',()=>{if(motionReason()){dismiss();reason=motionReason();}});
  if(!ready)document.addEventListener('DOMContentLoaded',()=>{ready=true;revealPending();},{once:true});
})();
