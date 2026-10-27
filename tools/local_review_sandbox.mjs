import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {randomBytes} from 'node:crypto';

export async function start(snapshot,root,python,reviewUrl){
 const expected=snapshot.recurring.map(row=>row.id).sort((a,b)=>a-b).join(',');
 const csrf=randomBytes(32).toString('hex'),cache=new Map();
 const child=spawn(python,['tools/local_review_sandbox.py'],{cwd:root,env:{PATH:process.env.PATH||'/usr/bin:/bin',...(process.env.LD_LIBRARY_PATH?{LD_LIBRARY_PATH:process.env.LD_LIBRARY_PATH}:{}),APP_ENV:'development',PRIVATE_BETA:'0',LANG:'en_US.UTF-8'}});
 const backend=await new Promise((resolve,reject)=>{let output='';child.stdout.on('data',chunk=>{output+=chunk;if(output.includes('\n')){try{resolve(JSON.parse(output.slice(0,output.indexOf('\n'))))}catch{reject(Error('Yerel çalışma kopyası başlatılamadı.'))}}});child.stderr.on('data',chunk=>{const safe=String(chunk).split('\n').filter(line=>line.startsWith('REVIEW_ERROR '));for(const line of safe)process.stderr.write(line+'\n')});child.on('error',reject);child.on('exit',()=>reject(Error('Yerel çalışma kopyası hazırlanamadı.')));child.stdin.end(JSON.stringify(snapshot));});
 snapshot=null;
 async function authenticated(cookie){if(!cookie)return false;if((cache.get(cookie)||0)>Date.now())return true;const response=await fetch(reviewUrl+'/api/recurring',{headers:{Cookie:cookie}});if(!response.ok)return false;const body=await response.json();if(body.rules.map(row=>row.id).sort((a,b)=>a-b).join(',')!==expected)return false;cache.set(cookie,Date.now()+60000);return true;}
 const server=http.createServer(async(req,res)=>{
  const json=(status,body)=>{res.writeHead(status,{'Content-Type':'application/json; charset=utf-8'});res.end(JSON.stringify(body))};
  res.setHeader('Cache-Control','no-store');res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('X-Frame-Options','DENY');res.setHeader('Referrer-Policy','no-referrer');res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; frame-ancestors 'none'; form-action 'self'");
  try{
   if(!/^localhost:\d+$/.test(req.headers.host||''))return json(403,{error:'Yalnızca localhost erişimi açık.'});
   const target=new URL(req.url,'http://'+req.headers.host),method=req.method;
   if(!['GET','POST','PUT'].includes(method))return json(405,{error:'İşlem desteklenmiyor.'});
   if(method!=='GET'&&req.headers.origin!=='http://'+req.headers.host)return json(403,{error:'İstek kaynağı doğrulanamadı.'});
   let body='';if(method!=='GET')for await(const chunk of req){body+=chunk;if(body.length>16000)return json(413,{error:'İstek boyutu aşıldı.'})}
   if(method==='POST'&&target.pathname==='/api/login'){
    const remote=await fetch(reviewUrl+'/api/login',{method:'POST',headers:{Origin:reviewUrl,'Content-Type':'application/json'},body});body='';
    if(!remote.ok){res.writeHead(remote.status,{'Content-Type':'application/json'});return res.end(await remote.text())}
    const cookies=remote.headers.getSetCookie();const cookie=cookies.map(c=>c.split(';')[0]).join('; ');
    if(!await authenticated(cookie))return json(403,{error:'Bu çalışma kopyası giriş yaptığınız hesaba ait değil.'});
    res.setHeader('Set-Cookie',cookies);return json(200,{csrf,local_sandbox:true});
   }
   if(method==='GET'&&(target.pathname==='/'||target.pathname.startsWith('/static/'))){
    const file=target.pathname==='/'?path.join(root,'static/index.html'):path.resolve(root,'.'+target.pathname);
    if(!file.startsWith(path.join(root,'static')+path.sep))return json(404,{});
    res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript; charset=utf-8':file.endsWith('.css')?'text/css; charset=utf-8':file.endsWith('.html')?'text/html; charset=utf-8':file.endsWith('.woff2')?'font/woff2':'application/octet-stream'});return res.end(await fs.readFile(file));
   }
   const valid=await authenticated(req.headers.cookie||'');
   if(method==='GET'&&target.pathname==='/api/session')return json(200,{user_id:valid?'local-review':null,csrf:valid?csrf:null,private_beta:false,local_sandbox:true});
   if(!valid)return json(401,{error:'Kendi hesabınıza giriş yapın.'});
   if(method!=='GET'&&req.headers['x-csrf-token']!==csrf)return json(403,{error:'Güvenlik doğrulaması başarısız.'});
   if(method==='POST'&&target.pathname==='/api/logout'){cache.delete(req.headers.cookie||'');res.setHeader('Set-Cookie','finance_read_only_review=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict');return json(200,{ok:true})}
   const allowed=method==='GET'?['/api/options','/api/profile','/api/simple-cashflow'].includes(target.pathname):method==='PUT'?['/api/profile','/api/simple-opening'].includes(target.pathname):['/api/profile/cards','/api/simple-opening','/api/simple-statement-payments','/api/simple-payment-plans','/api/simple-income','/api/simple-income/receive'].includes(target.pathname)||/^\/api\/simple-payments\/\d+\/complete$/.test(target.pathname)||/^\/api\/recurring\/\d+\/status$/.test(target.pathname);
   if(!allowed)return json(405,{error:'Bu işlem yerel inceleme kapsamı dışında.'});
   const response=await fetch(backend.url+target.pathname+target.search,{method,headers:{Cookie:backend.cookie,'X-CSRF-Token':backend.csrf,'Content-Type':'application/json'},...(method==='GET'?{}:{body})});
   res.writeHead(response.status,{'Content-Type':'application/json; charset=utf-8'});res.end(await response.text());
  }catch{return json(503,{error:'Yerel çalışma kopyası yüklenemedi. Oturumunu kontrol et.'})}
 });
 await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve)});
 return {url:'http://localhost:'+server.address().port,close:()=>{server.close();child.kill()}};
}
