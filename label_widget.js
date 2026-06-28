// ==============================================================================
// label_widget.js -- CANONICAL per-box FP/correct/dup labeling widget.
// EXTRACTED VERBATIM from generate_timeline.py (line 248 + lines 305-398).
// Single source of truth: the event contact-sheet wall REUSES this (no reinvented markVerdict).
// Requires DOM: #lightbox, #lb-wrap, #lightbox-img, #lightbox-note, #lblcount, and .lbbox CSS;
// thumbnails must carry data-id / data-dets / data-boxes / data-full + onclick="openFull(this)".
// _lbl schema (gen_timeline line 355): {id,box,cls,conf,bbox,verdict,ts}; verdict in {'ok','fp','dup'}.
// NOTE: the trailing '});' closes the DOMContentLoaded listener (line 387) -- in generate_timeline
//       that same listener continues into a timeline-only image-load queue we intentionally omit.
// ==============================================================================
var GMM_SINK='https://labels.proxyforanding.xyz';  // click -> auto-saved to GitHub; '' to disable
var GMM_LABELS=JSON.parse(localStorage.getItem('gmm_labels')||'[]');
window._curImg=null;window._selBox=null;
function vName(v){return v==='fp'?'誤報':v==='dup'?'重複(多框)':'正確';}
function vColor(v){return v==='fp'?'#e74c3c':v==='dup'?'#ff9800':'#2ecc71';}
function gmmCount(){var e=document.getElementById('lblcount');if(e)e.textContent=GMM_LABELS.length;}
function _exist(i){var m=GMM_LABELS.filter(function(x){return window._curImg&&x.id===window._curImg.id&&x.box===i;});return m.length?m[0].verdict:null;}
function openFull(el){
  var idl=el.getAttribute('data-id')||'';
  var dets=el.getAttribute('data-dets')||'';
  var boxes=[];try{boxes=JSON.parse(el.getAttribute('data-boxes')||'[]');}catch(e){}
  var info=idl+(dets?('  ['+dets+']'):'');
  window._curImg={id:idl,dets:dets,boxes:boxes};window._selBox=null;
  var full=el.getAttribute('data-full');
  if(!full){window.open(el.src,'_blank');return;}
  var im=document.getElementById('lightbox-img');
  var note=document.getElementById('lightbox-note');
  im.src=el.src;
  note.textContent=info+'  · 點圖上的框 -> 選 正確/誤報/重複';
  document.getElementById('lightbox').style.display='flex';
  im.onload=function(){renderBoxes();};
  renderBoxes();
  var hi=new Image();
  hi.onload=function(){im.src=full;renderBoxes();};
  hi.src=full;
}
function renderBoxes(){
  var wrap=document.getElementById('lb-wrap');if(!wrap||!window._curImg)return;
  [].slice.call(wrap.querySelectorAll('.lbbox')).forEach(function(e){e.remove();});
  (window._curImg.boxes||[]).forEach(function(bx,i){
    var b=bx.b,d=document.createElement('div');d.className='lbbox';
    d.style.left=(b[0]*100)+'%';d.style.top=(b[1]*100)+'%';
    d.style.width=((b[2]-b[0])*100)+'%';d.style.height=((b[3]-b[1])*100)+'%';
    var ex=_exist(i);
    if(ex){d.style.borderColor=vColor(ex);d.style.borderStyle='solid';}
    if(i===window._selBox){d.style.boxShadow='0 0 0 3px #fff';}
    d.title=bx.c+' '+bx.p+'%'+(ex?(' = '+vName(ex)):'');
    d.onclick=function(ev){ev.stopPropagation();selectBox(i);};
    wrap.appendChild(d);
  });
}
function selectBox(i){
  window._selBox=i;renderBoxes();
  var b=window._curImg.boxes[i];
  document.getElementById('lightbox-note').textContent=
    window._curImg.id+'  -> 已選 框#'+(i+1)+' '+b.c+' '+b.p+'%（請按下方 正確/誤報/重複）';
}
function verdict(v){
  if(window._selBox===null){alert('先點圖上要標的那個框');return;}
  var i=window._selBox,b=window._curImg.boxes[i];
  GMM_LABELS=GMM_LABELS.filter(function(x){return !(x.id===window._curImg.id&&x.box===i);});
  var _lbl={id:window._curImg.id,box:i,cls:b.c,conf:b.p,bbox:b.b,verdict:v,ts:Date.now()};
  GMM_LABELS.push(_lbl);
  localStorage.setItem('gmm_labels',JSON.stringify(GMM_LABELS));
  if(GMM_SINK){fetch(GMM_SINK+'/label',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(_lbl)}).catch(function(){});} // auto-save; localStorage is the fallback
  gmmCount();renderBoxes();
  document.getElementById('lightbox-note').textContent=
    window._curImg.id+'  框#'+(i+1)+' '+b.c+' -> '+vName(v)+' ✓（可繼續點別的框）';
}
function copyId(){
  if(!window._curImg)return;var t=window._curImg.id;
  var done=function(){document.getElementById('lightbox-note').textContent='已複製編號: '+t;};
  if(navigator.clipboard){navigator.clipboard.writeText(t).then(done,function(){prompt('複製這個編號：',t);});}
  else{prompt('複製這個編號：',t);}
}
function closeLightbox(){document.getElementById('lightbox').style.display='none';
  document.getElementById('lightbox-img').src='';window._selBox=null;}
function retryImg(el){
  var n=parseInt(el.dataset.retries||'0');
  if(n>=3)return;
  el.dataset.retries=n+1;
  setTimeout(function(){el.src=el.src.split('?')[0]+'?r='+Date.now();},1500*(n+1));
}
function exportLabels(){
  if(!GMM_LABELS.length){alert('還沒有任何標記');return;}
  var blob=new Blob([JSON.stringify(GMM_LABELS,null,1)],{type:'application/json'});
  var a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='labels_'+(location.pathname.split('/').pop()||'timeline')+'.json';
  document.body.appendChild(a);a.click();a.remove();
}
function clearLabels(){if(confirm('清除本機所有標記（'+GMM_LABELS.length+'）？')){GMM_LABELS=[];localStorage.setItem('gmm_labels','[]');gmmCount();}}
// Controlled loading: visible cells immediately, background queue daytime-first.
// Concurrency capped at 3 so flaky proxy paths are never flooded.
document.addEventListener('DOMContentLoaded',function(){
  // zero-action recovery: push any localStorage labels not yet in the sink (e.g. ones made
  // before the sink existed) so the user never has to export
  if(GMM_SINK&&GMM_LABELS.length){
    fetch(GMM_SINK+'/labels').then(function(r){return r.json();}).then(function(rem){
      var seen={};(Array.isArray(rem)?rem:[]).forEach(function(x){seen[x.id+'|'+x.box]=1;});
      var pend=GMM_LABELS.filter(function(x){return !seen[x.id+'|'+x.box];});
      (function up(k){if(k>=pend.length)return;
        fetch(GMM_SINK+'/label',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(pend[k])}).then(function(){up(k+1);}).catch(function(){});})(0);
    }).catch(function(){});
  }
});
