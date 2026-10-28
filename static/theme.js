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
      const label=dark?'Açık temaya geç':'Koyu temaya geç';
      button.setAttribute('aria-label',label);button.setAttribute('title',label);button.setAttribute('aria-pressed',String(dark));
      const icon=button.querySelector('.theme-icon'),text=button.querySelector('.theme-label');
      if(icon)icon.textContent=dark?'🌙':'☀️';if(text)text.textContent=dark?'Koyu tema':'Açık tema';
    });
  }
  apply();
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
