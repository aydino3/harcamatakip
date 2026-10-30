'use strict';
const {before,after,test}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path'),net=require('node:net');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
let temp,server,browser,url;
before(async()=>{
  temp=await fs.mkdtemp(path.join(os.tmpdir(),'privacy-e2e-'));
  const handle=net.createServer();await new Promise(resolve=>handle.listen(0,'127.0.0.1',resolve));const port=handle.address().port;await new Promise(resolve=>handle.close(resolve));
  url=`http://127.0.0.1:${port}`;
  server=spawn(process.env.PYTHON_BIN||'python3',['tools/privacy_local_review.py'],{cwd:root,env:{...process.env,APP_ENV:'development',VERCEL:'',PRIVATE_BETA:'0',EXPENSE_DATABASE_URL:'',DATABASE_URL:'',POSTGRES_URL:'',PRIVACY_NOTICE_PATH:'',EXPENSE_DB_PATH:path.join(temp,'synthetic.sqlite3'),APP_PORT:String(port)},stdio:'ignore'});
  const deadline=Date.now()+15000;let ready=false;while(Date.now()<deadline){try{if((await fetch(url+'/healthz')).ok){ready=true;break}}catch{}await new Promise(resolve=>setTimeout(resolve,100))}assert.ok(ready,'Yerel test sunucusu başlamalı');
  browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH?{executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH}:{})});
});
after(async()=>{await browser?.close();server?.kill();if(temp)await fs.rm(temp,{recursive:true,force:true})});

test('KVKK: boş kutu, iki dil, kayıt/giriş ve hesaba bağlı beyan',async()=>{
 const context=await browser.newContext({viewport:{width:390,height:844}}),page=await context.newPage();
 try{
  await page.goto(url);
  const form=page.locator('#auth-form'),box=form.locator('[name=privacy_read]'),submit=form.locator('button.primary');
  await assert.doesNotReject(()=>box.waitFor());assert.equal(await box.isChecked(),false);assert.equal(await box.isDisabled(),true);assert.equal(await submit.isDisabled(),true);
  await form.locator('[name=identifier]').fill('privacy.user');await form.locator('[name=password]').fill('synthetic-privacy-password');
  await page.getByRole('button',{name:'English',exact:true}).click();
  assert.equal(await form.locator('[name=identifier]').inputValue(),'privacy.user');assert.equal(await box.isChecked(),false);
  await page.locator('#open-privacy').click();await page.locator('#privacy-dialog').waitFor({state:'visible'});
  await page.locator('#finish-privacy').click();assert.equal(await box.isChecked(),false);assert.equal(await submit.isDisabled(),true);
  await box.check();assert.equal(await submit.isEnabled(),true);
  await page.getByRole('button',{name:'Türkçe',exact:true}).click();assert.equal(await box.isChecked(),false);assert.equal(await box.isDisabled(),true);
  await page.locator('#toggle-auth').click();await form.locator('[name=username]').fill('privacy.user');await form.locator('[name=email]').fill('privacy@example.invalid');await form.locator('[name=password]').fill('synthetic-privacy-password');
  await page.locator('#open-privacy').click();await page.locator('#close-privacy').click();await box.check();await submit.click();await page.locator('#open-profile').waitFor();
  const receipt=await(await context.request.get(url+'/api/privacy-acknowledgments')).json();assert.equal(receipt.acknowledgments.length,1);assert.equal(receipt.acknowledgments[0].action,'register');
  await page.locator('#logout').click();await box.waitFor();assert.equal(await box.isChecked(),false);
  await form.locator('[name=identifier]').fill('privacy.user');await form.locator('[name=password]').fill('synthetic-privacy-password');await page.locator('#open-privacy').click();await page.locator('#finish-privacy').click();await box.check();await submit.click();await page.locator('#open-profile').waitFor();
  const receipts=await(await context.request.get(url+'/api/privacy-acknowledgments')).json();assert.deepEqual(new Set(receipts.acknowledgments.map(row=>row.action)),new Set(['register','login']));
 }finally{await context.close()}
});
