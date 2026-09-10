#!/usr/bin/env node
// Private JSON-lines transport. Never echo commands, credentials or page text.
const readline = require('readline');
const engine = require('./facebook_followed_video_engine');
const {perform} = require('./facebook_session');
const interactive = require('./facebook_interactive_browser');
const emit = value => console.log('HM_ACCOUNT_RESULT ' + JSON.stringify(value));
async function main() {
  interactive.privatePreferences(process.argv[2]);
  const browser = await engine.startBrowser(process.argv[2]);
  try {
    for await (const line of readline.createInterface({input: process.stdin})) {
      let input;
      try {
        input = JSON.parse(line);
        if (input.action === 'close') break;
        if(input.action==='browser'){await interactive.open(browser);emit({state:'BROWSER_READY'});continue;}
        if(input.action==='view'){
          try {console.log('HM_BROWSER_RESULT '+JSON.stringify({requestId:input.requestId,...await interactive.command(browser,input)}));}
          catch(_){console.log('HM_BROWSER_RESULT '+JSON.stringify({requestId:input.requestId,error:'BROWSER_UNAVAILABLE'}));}
          continue;
        }
        if (!['login', 'verify', 'code', 'check', 'finish'].includes(input.action)) throw Error('Invalid action');
        if (input.action === 'code' && !/^\d{6}$/.test(input.oneTimeCode || '')) throw Error('Invalid code');
        const result = await perform(browser, input);
        emit({state: result.state, reasonCode: result.reasonCode || null,
          requiresCode: result.state === 'VERIFICATION_REQUIRED' && result.reasonCode !== 'FACEBOOK_ACCOUNT_SUSPENDED'
            && /authentication app|authenticator app|6-digit code|身份验证器|身分驗證器/i.test(result.errorText || '')});
      } catch (_) {
        emit({state: 'COOLDOWN', reasonCode: 'SESSION_CHECK_FAILED', requiresCode: false});
      } finally { input = null; }
    }
  } finally { browser.ws.close(); await engine.stopChrome(browser.chrome); }
}
if (require.main === module) main().catch(() => {emit({state: 'COOLDOWN', reasonCode: 'SESSION_CHECK_FAILED'});process.exitCode = 1;});
