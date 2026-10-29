/* Device-aware visual preference; no account or financial state is changed. */
(function(){
  'use strict';
  const key='finance.theme';
  const system=typeof window.matchMedia==='function'?window.matchMedia('(prefers-color-scheme: dark)'):null;
  let preference=null;
  try{const saved=localStorage.getItem(key);if(saved==='light'||saved==='dark')preference=saved}catch{}
  function apply(){
    const theme=preference||(system?.matches?'dark':'light'),dark=theme==='dark';
    document.documentElement.dataset.theme=theme;
    const meta=document.querySelector('meta[name="theme-color"]');if(meta)meta.content=dark?'#0b101b':'#f3f5fb';
    document.querySelectorAll('[data-theme-toggle]').forEach(button=>{
      const key=dark?'Açık temaya geç':'Koyu temaya geç';const label=window.FinanceI18n?window.FinanceI18n.t(key):key;
      button.setAttribute('aria-label',label);button.setAttribute('title',label);button.setAttribute('aria-pressed',String(dark));
    });
  }
  apply();
  document.addEventListener('finance-language-change',apply);
  document.addEventListener('click',event=>{
    if(!event.target.closest?.('[data-theme-toggle]'))return;
    preference=document.documentElement.dataset.theme==='dark'?'light':'dark';
    try{localStorage.setItem(key,preference)}catch{}
    apply();
  });
  const changed=()=>{if(!preference)apply()};
  if(system?.addEventListener)system.addEventListener('change',changed);else if(system?.addListener)system.addListener(changed);
  window.addEventListener('storage',event=>{if(event.key!==key&&event.key!==null)return;preference=event.newValue==='light'||event.newValue==='dark'?event.newValue:null;apply()});
})();
