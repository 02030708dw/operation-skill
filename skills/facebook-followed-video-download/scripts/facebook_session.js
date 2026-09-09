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
async function inspect(ws) {
  const page=await engine.cdpCall(ws,{id:seq++,method:'Runtime.evaluate',params:{expression:`JSON.stringify({url:location.href, login:!!document.querySelector('input[name="pass"]'), challenge: /checkpoint|two_step_verification|recover|challenge/.test(location.pathname), loaded:document.readyState==='complete', text:document.body.innerText})`,returnByValue:true}});
  const data=JSON.parse(page.result.result.value);
  const response=await engine.cdpCall(ws,{id:seq++,method:'Network.getCookies',params:{urls:['https://www.facebook.com/']}});
  const cookie=response.result.cookies.find(c=>c.name==='c_user');
  if(data.challenge) return {state:'VERIFICATION_REQUIRED',reasonCode:'FACEBOOK_CHECKPOINT'};
  if(data.login || /\/login/.test(new URL(data.url).pathname)) return {state:'LOGIN_REQUIRED',reasonCode:loginReason(data.text)};
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
    if(input.action==='login') {
      await evaluate(`(()=>{const email=document.querySelector('input[name="email"]');const pass=document.querySelector('input[name="pass"]');if(!email||!pass)return false;const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set;set.call(email,${JSON.stringify(input.username)});set.call(pass,${JSON.stringify(input.password)});for(const e of [email,pass]){e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));}const submit=document.querySelector('button[name="login"],input[name="login"]');if(submit)submit.click();else pass.form.requestSubmit();return true;})()`);
      await sleep(8000);
      if(input.twoFactorSecret) {
        const code=totp(input.twoFactorSecret);
        await evaluate(`(()=>{const e=document.querySelector('input[name="approvals_code"],input[autocomplete="one-time-code"]');if(!e)return false;Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(e,${JSON.stringify(code)});e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));const button=[...document.querySelectorAll('button,input[type="submit"],[role="button"]')].find(b=>/^(continue|submit|confirm|next|继续|繼續|ยืนยัน|ดำเนินการต่อ)$/i.test((b.innerText||b.value||'').trim()));if(button)button.click();return true;})()`);
        await sleep(7000);
      }
    }
    console.log(JSON.stringify(await inspect(browser.ws))); 
  } finally { browser.ws.close(); await engine.stopChrome(browser.chrome); }
}
if(require.main===module) main().catch(()=>{console.log(JSON.stringify({state:'COOLDOWN',reason:'SESSION_CHECK_FAILED'}));process.exitCode=1;});
module.exports={totp,loginReason};
