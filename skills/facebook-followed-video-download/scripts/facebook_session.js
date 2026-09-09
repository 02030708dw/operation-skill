#!/usr/bin/env node
// Normal account login only. Credentials arrive on stdin and are never logged.
const fs = require('fs');
const crypto = require('crypto');
const engine = require('./facebook_followed_video_engine.js');
let seq = 90000;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function totp(secret, now = Date.now()) {
  const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits='';
  for(const c of secret.toUpperCase().replace(/[\s=]/g,'')) {
    const n=alphabet.indexOf(c); if(n<0) throw new Error('Invalid two-factor secret');
    bits+=n.toString(2).padStart(5,'0');
  }
  const key=Buffer.from((bits.match(/.{8}/g)||[]).map(b=>parseInt(b,2)));
  const counter=Buffer.alloc(8);counter.writeBigUInt64BE(BigInt(Math.floor(now/30000)));
  const hash=crypto.createHmac('sha1',key).update(counter).digest();
  return String((hash.readUInt32BE(hash[19]&15)&0x7fffffff)%1000000).padStart(6,'0');
}
function loginReason(text) {
  if (/can't find your account|couldn't find your account|isn't connected to an account|not connected to an account|找不到.*帐?账户|找不到.*帳號|没有.*关联|does not match an account/i.test(text)) return 'FACEBOOK_ACCOUNT_NOT_FOUND';
  if (/incorrect password|password.*incorrect|wrong password|密码.*错误|密碼.*錯誤/i.test(text)) return 'FACEBOOK_PASSWORD_REJECTED';
  if (/temporarily blocked|try again later|too many attempts|暂时.*封|稍后重试/i.test(text)) return 'FACEBOOK_LOGIN_RATE_LIMITED';
  if (/check your notifications|another device|其他设备/i.test(text)) return 'FACEBOOK_DEVICE_APPROVAL_REQUIRED';
  return 'FACEBOOK_LOGIN_NOT_COMPLETED';
}
async function waitForPage(ws) {
  const deadline=Date.now()+30000;
  while(Date.now()<deadline) {
    try {
      const page=await engine.cdpCall(ws,{id:seq++,method:'Runtime.evaluate',params:{expression:"!!document.body && document.readyState !== 'loading'",returnByValue:true}},5000);
      if(page.result.result.value) return;
    } catch (_) { /* Navigation can replace the execution context. */ }
    await sleep(1000);
  }
}
async function inspect(ws) {
  await waitForPage(ws);
  const page=await engine.cdpCall(ws,{id:seq++,method:'Runtime.evaluate',params:{expression:`JSON.stringify({url:location.href, login:!!document.querySelector('input[name="pass"]'), challenge: /checkpoint|two_step_verification|recover|challenge/.test(location.pathname), loaded:document.readyState==='complete' && !!document.body, text:document.body?.innerText||'', errorText:[...document.querySelectorAll("#error_box,.login_error_box,[role=alert]")].map(e=>e.innerText).join(" ").slice(0,500)})`,returnByValue:true}});
  const data=JSON.parse(page.result.result.value);
  const response=await engine.cdpCall(ws,{id:seq++,method:'Network.getCookies',params:{urls:['https://www.facebook.com/']}});
  const cookie=response.result.cookies.find(c=>c.name==='c_user');
  if(data.challenge) return {state:'VERIFICATION_REQUIRED',reasonCode:'FACEBOOK_CHECKPOINT',errorText:data.text.slice(0,900)};
  if(data.login || /\/login/.test(new URL(data.url).pathname)) return {state:'LOGIN_REQUIRED',reasonCode:loginReason(data.text),errorText:data.errorText};
  if(data.loaded && cookie && new URL(data.url).hostname==='www.facebook.com') return {state:'AVAILABLE'};
  return {state:'COOLDOWN',reasonCode:'SESSION_CHECK_INCONCLUSIVE'};
}
async function main() {
  const input=JSON.parse(fs.readFileSync(0,'utf8'));
  const browser=await engine.startBrowser(input.profile);
  const call=(method,params)=>engine.cdpCall(browser.ws,{id:seq++,method,params});
  const evaluate=async expression=>call('Runtime.evaluate',{expression,returnByValue:true});
  try {
    await call('Page.navigate',{url:input.action==='login'?'https://www.facebook.com/login/':'https://www.facebook.com/me/'});
    await sleep(6000);
    await waitForPage(browser.ws);
    if(input.action==='login') {
      async function enter(selector,text) {
        const focused=await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)return false;e.focus();e.select();return true;})()`);
        if(!focused.result.result.value) return false;
        await call('Input.insertText',{text});return true;
      }
      const emailEntered=await enter('input[name="email"]',input.username);
      const passwordEntered=await enter('input[name="pass"]',input.password);
      if(!emailEntered || !passwordEntered) {
        console.log(JSON.stringify(await inspect(browser.ws)));return;
      }
      const submitted=await evaluate(`(()=>{const password=document.querySelector('input[name="pass"]');const form=password?.form;const visible=[...document.querySelectorAll('button,[role="button"],input[type="submit"]')].find(e=>e.getClientRects().length && /^(log in|login|登录|登入|เข้าสู่ระบบ)$/i.test((e.innerText||e.value||'').trim()));const submit=visible||(form||document).querySelector('button[name="login"],input[name="login"],button[type="submit"],input[type="submit"]');if(submit){submit.click();return true;}if(form){form.requestSubmit();return true;}return false;})()`);
      if(!submitted.result.result.value) {
        console.log(JSON.stringify({state:'LOGIN_REQUIRED',reasonCode:'FACEBOOK_LOGIN_NOT_COMPLETED',errorText:'Login form submit control was not found.'}));return;
      }
      await sleep(8000);
      await waitForPage(browser.ws);
      if(input.twoFactorSecret) {
        const code=totp(input.twoFactorSecret);
        // Current Facebook uses an unnamed input on its authenticator-app page.
        // Do not mistake another checkpoint (phone, identity, etc.) for a TOTP form.
        await evaluate(`(()=>{if(!/authentication app|authenticator app|two-factor authentication app|身份验证器|身分驗證器/i.test(document.body?.innerText||''))return;const input=[...document.querySelectorAll('input')].find(e=>e.getClientRects().length && ['text','tel','number'].includes(e.type) && e.name!=='email');if(input)input.setAttribute('data-hermes-auth-code','true');})()`);
        const codeEntered=await enter('input[name="approvals_code"],input[autocomplete="one-time-code"],input[data-hermes-auth-code="true"]',code);
        if(codeEntered) {
          await evaluate(`(()=>{const button=[...document.querySelectorAll('button,input[type="submit"],[role="button"]')].find(b=>b.getClientRects().length && /^(continue|submit|confirm|next|继续|繼續|ยืนยัน|ดำเนินการต่อ)$/i.test((b.innerText||b.value||'').trim()));if(button)button.click();})()`);
          await sleep(7000);
        }
      }
    }
    const result=await inspect(browser.ws);
    if(result.errorText){for(const secret of [input.username,input.password,input.twoFactorSecret])if(secret)result.errorText=result.errorText.split(secret).join('[hidden]');}
    console.log(JSON.stringify(result));
  } finally { browser.ws.close(); await engine.stopChrome(browser.chrome); }
}
if(require.main===module) main().catch(error=>{console.log(JSON.stringify({state:'COOLDOWN',reasonCode:error.code||'SESSION_CHECK_FAILED',errorType:error.name}));process.exitCode=1;});
module.exports={totp,loginReason,waitForPage};
