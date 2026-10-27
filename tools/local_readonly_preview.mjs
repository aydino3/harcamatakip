'use strict';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawn} from 'node:child_process';

export async function start(snapshot,root,python,reviewUrl){
  snapshot={...snapshot,opening:Array.isArray(snapshot.opening)?snapshot.opening[0]||null:snapshot.opening,payment_events:[]};
  const expectedIds=snapshot.recurring.map(row=>row.id).sort((a,b)=>a-b).join(',');
  const cache=new Map();
  async function authenticate(cookie){
    if(!cookie)return null;
    const cached=cache.get(cookie);if(cached&&cached.until>Date.now())return cached.options;
    const [opts,rules]=await Promise.all([fetch(reviewUrl+'/api/options',{headers:{Cookie:cookie}}),fetch(reviewUrl+'/api/recurring',{headers:{Cookie:cookie}})]);
    if(!opts.ok||!rules.ok)return null;
    const [options,recurring]=await Promise.all([opts.json(),rules.json()]);
    if(recurring.rules.map(row=>row.id).sort((a,b)=>a-b).join(',')!==expectedIds)return null;
    cache.set(cookie,{options,until:Date.now()+60000});return options;
  }
  function compute(start,end){return new Promise((resolve,reject)=>{
    const code="import json,sys; import simple_cashflow as f; d=json.load(sys.stdin); r=f.calculate(d,sys.argv[1],sys.argv[2]); r.update(rules=[dict(x,revision=f.rule_revision(x)) for x in d['recurring']],income_rules=d['income_rules'],income_records=d['income_records'],income_rule_amounts=d['income_rule_amounts'],read_only_preview=True); r['net_minor']=str(r['net_minor']); r['totals']={k:str(v) for k,v in r['totals'].items()}; [(e.update({k:str(e[k]) for k in ('amount_minor','net_after_minor','cash_effect_minor')})) for e in r['events']]; print(json.dumps(r,ensure_ascii=False))";
    const child=spawn(python,['-c',code,start,end],{cwd:root,env:{PATH:'/usr/bin:/bin:/opt/homebrew/bin',APP_ENV:'development',EXPENSE_DATABASE_URL:'',POSTGRES_URL:'',DATABASE_URL:'',VERCEL:'',LANG:'en_US.UTF-8'}});
    let output='';child.stdout.on('data',chunk=>output+=chunk);child.stderr.on('data',()=>{});child.on('error',reject);child.on('close',exit=>exit===0?resolve(output):reject(Error('Tarih aralığı hesaplanamadı.')));child.stdin.end(JSON.stringify(snapshot));
  })}
  const server=http.createServer(async(req,res)=>{
    const json=(code,value)=>{res.writeHead(code,{'Content-Type':'application/json; charset=utf-8'});res.end(JSON.stringify(value))};
    res.setHeader('Cache-Control','no-store');res.setHeader('Referrer-Policy','no-referrer');res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; frame-ancestors 'none'; form-action 'self'");
    try{
      if(!/^localhost:\d+$/.test(req.headers.host||''))return json(403,{error:'Yalnızca yerel erişim açık.'});
      const target=new URL(req.url,'http://'+req.headers.host);
      if(req.method==='POST'&&req.headers.origin!=='http://'+req.headers.host)return json(403,{error:'İstek kaynağı doğrulanamadı.'});
      if(req.method==='POST'&&target.pathname==='/api/login'){
        let body='';for await(const chunk of req){body+=chunk;if(body.length>16000)return json(413,{error:'İstek boyutu aşıldı.'})}
        const remote=await fetch(reviewUrl+'/api/login',{method:'POST',headers:{Origin:reviewUrl,'Content-Type':'application/json'},body});
        for(const cookie of remote.headers.getSetCookie())res.setHeader('Set-Cookie',cookie);
        res.writeHead(remote.status,{'Content-Type':'application/json; charset=utf-8'});return res.end(await remote.text());
      }
      if(req.method==='POST'&&target.pathname==='/api/logout'){
        cache.delete(req.headers.cookie||'');res.setHeader('Set-Cookie','finance_read_only_review=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict');return json(200,{ok:true});
      }
      if(req.method!=='GET')return json(405,{error:'Salt okunur önizleme: canlı kayıtlara yazma kapalı.'});
      if(target.pathname==='/'){
        res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});return res.end('<!doctype html><html lang="tr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Gelir ve ödeme planı · Yerel önizleme</title><link rel="stylesheet" href="/static/style.css"><body><div id="app"></div><script src="/static/app.js"></script></body></html>');
      }
      if(['/static/app.js','/static/style.css'].includes(target.pathname)){
        res.writeHead(200,{'Content-Type':target.pathname.endsWith('.js')?'text/javascript; charset=utf-8':'text/css; charset=utf-8'});return res.end(await fs.readFile(path.join(root,target.pathname)));
      }
      if(target.pathname.startsWith('/static/')&&!target.pathname.includes('..')){
        const file=path.resolve(root,'.'+target.pathname);if(!file.startsWith(path.join(root,'static')+path.sep))return json(404,{});res.writeHead(200);return res.end(await fs.readFile(file));
      }
      const options=await authenticate(req.headers.cookie||'');
      if(target.pathname==='/api/session')return json(200,options?{user_id:'readonly-preview',csrf:'readonly',private_beta:false}:{user_id:null,private_beta:false});
      if(!options)return json(401,{error:'Önce kendi hesabınıza giriş yapın.'});
      if(target.pathname==='/api/options')return json(200,options);
      if(target.pathname==='/api/profile'){const p=snapshot.profile;if(!p)return json(503,{error:'Profil bilgisi yüklenemedi.'});return json(200,{...p,cards:options.credit_cards.map(card=>({id:'legacy:'+card.id,bank_name:card.bank_name,display_name:card.display_name,last_four:card.last_four,active:card.active,payment_due_on:p.known_due_dates?.[String(card.id)]||null,version:0,monthly_due_day:card.due_day}))})}
      if(target.pathname==='/api/simple-cashflow'){
        const start=target.searchParams.get('start'),end=target.searchParams.get('end');
        if(!/^\d{4}-\d{2}-\d{2}$/.test(start||'')||!/^\d{4}-\d{2}-\d{2}$/.test(end||''))return json(400,{error:'Tarih aralığını kontrol edin.'});
        const result=await compute(start,end);res.writeHead(200,{'Content-Type':'application/json; charset=utf-8'});return res.end(result);
      }
      return json(404,{error:'Bu önizlemede bu işlem kapalı.'});
    }catch{return json(503,{error:'Önizleme yüklenemedi. Oturumunuzu ve tarih aralığını kontrol edin.'})}
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve)});
  return {url:'http://localhost:'+server.address().port,close:()=>new Promise(resolve=>server.close(resolve))};
}
