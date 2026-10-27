import {test} from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import path from 'node:path';
import {start} from '../tools/local_review_sandbox.mjs';

test('Yerel çalışma kopyası: sahiplik, CSRF ve canlıya yazmama',async()=>{
 const calls=[];
 const source=http.createServer((req,res)=>{calls.push(req.method+' '+req.url);res.setHeader('Content-Type','application/json');if(req.url==='/api/login'){res.setHeader('Set-Cookie','finance_read_only_review=synthetic-review; Path=/; HttpOnly');res.end('{}')}else if(req.url==='/api/recurring'&&req.headers.cookie?.includes('synthetic-review'))res.end(JSON.stringify({rules:[{id:1}]}));else{res.statusCode=401;res.end('{}')}});
 await new Promise(resolve=>source.listen(0,'127.0.0.1',resolve));
 const root=path.resolve(import.meta.dirname,'..'),python=process.env.PYTHON_BIN||'python3';
 const snapshot={profile:{email:'local-copy@example.invalid',display_name:'Yerel test',known_due_dates:{}},categories:[],payment_methods:[],credit_cards:[],statements:[],recurring:[{id:1,description:'Sentetik banka ödemesi',amount_minor:1000,frequency:'monthly',start_on:'2024-01-01',end_on:'2024-02-01',status:'active',category_id:null,payment_method_id:null}],income_rules:[],income_rule_amounts:[],income_records:[],expenses:[],occurrences:[],repayments:[],adjustments:[],opening:[]};
 let instance;
 try{
  instance=await start(snapshot,root,python,'http://127.0.0.1:'+source.address().port);
  const response=await fetch(instance.url+'/api/profile');assert.equal(response.status,401);
  assert.equal((await fetch(instance.url+'/api/login',{method:'POST',headers:{Origin:'https://untrusted.invalid','Content-Type':'application/json'},body:'{}'})).status,403);
  const login=await fetch(instance.url+'/api/login',{method:'POST',headers:{Origin:instance.url,'Content-Type':'application/json'},body:JSON.stringify({email:'local-copy@example.invalid',password:'synthetic-only'})});
  assert.equal(login.status,200);const session=await login.json();const cookie=login.headers.getSetCookie().map(value=>value.split(';')[0]).join('; ');
  const headers={Origin:instance.url,Cookie:cookie,'Content-Type':'application/json','X-CSRF-Token':session.csrf};
  assert.equal((await fetch(instance.url+'/api/profile',{method:'PUT',headers:{...headers,'X-CSRF-Token':'incorrect'},body:JSON.stringify({display_name:'Engellenmeli'})})).status,403);
  assert.equal((await fetch(instance.url+'/api/profile',{method:'PUT',headers,body:JSON.stringify({display_name:'Kaydedilen yerel ad'})})).status,200);
  assert.equal((await (await fetch(instance.url+'/api/profile',{headers})).json()).display_name,'Kaydedilen yerel ad');
  const cash=await (await fetch(instance.url+'/api/simple-cashflow?start=2024-01-01&end=2024-02-28',{headers})).json();assert.equal(cash.local_sandbox,true);assert.equal(cash.net_minor,'-2000');
  const bank={entry_kind:'regular',description:'Haftalık deneme',amount:'25',start_on:'2024-01-02',end_on:'2024-01-23',frequency:'weekly',card_ref:'',request_key:'proxy-bank-key-0001'};
  assert.equal((await fetch(instance.url+'/api/simple-payment-plans',{method:'POST',headers,body:JSON.stringify(bank)})).status,200);
  const next=await (await fetch(instance.url+'/api/simple-cashflow?start=2024-01-01&end=2024-02-28',{headers})).json();assert.equal(next.net_minor,'-12000');
  assert.equal((await fetch(instance.url+'/api/expenses',{method:'POST',headers,body:'{}'})).status,405);
  assert.ok(calls.every(call=>['POST /api/login','GET /api/recurring'].includes(call)));
 }finally{instance?.close();await new Promise(resolve=>source.close(resolve))}
});
