import json
import os
from pathlib import Path
import sys
import queue
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import hm_facebook_web_session as web
import hm_facebook_account as accounts
import hm_server_worker as worker

class WebSessionTests(unittest.TestCase):
    def test_two_step_login_promotes_only_after_code_and_cancellation_keeps_old_session(self):
        reports=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                reports.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body=b'{"code":200}';self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*_):pass
        server=HTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            for mode,cancel in [('login',False),('login',True),('browser',False),('browser',True)]:
                with self.subTest(mode=mode,cancel=cancel),tempfile.TemporaryDirectory() as folder:
                    root=Path(folder);bin_dir=root/'bin';bin_dir.mkdir()
                    node=bin_dir/'node'
                    node.write_text('#!'+sys.executable+'\n'+'''import sys,json
from pathlib import Path
for line in sys.stdin:
 command=json.loads(line)
 if command['action']=='close':break
 if command['action']=='browser':
  print('HM_ACCOUNT_RESULT '+json.dumps({'state':'BROWSER_READY'}),flush=True)
 elif command['action']=='view':
  print('HM_BROWSER_RESULT '+json.dumps({'requestId':command['requestId'],'image':'YQ==','width':1100,'height':700}),flush=True)
 elif command['action']=='login':
  print('non-result startup message\\nHM_ACCOUNT_RESULT '+json.dumps({'state':'VERIFICATION_REQUIRED','reasonCode':'FACEBOOK_CHECKPOINT','requiresCode':True}),flush=True)
 else:
  (Path(sys.argv[2])/'Cookies').write_text('verified-session')
  print('HM_ACCOUNT_RESULT '+json.dumps({'state':'AVAILABLE'}),flush=True)
''');node.chmod(0o700)
                    registry=root/'tenants.json';registry.write_text(json.dumps({'th':{'backendUrl':'http://127.0.0.1:'+str(server.server_port),'workerToken':'test','facebookAccount':{'key':'test'}}}))
                    profile=root/'accounts/test/profile';profile.mkdir(parents=True);(profile/'Cookies').write_text('original-session')
                    env=dict(os.environ,PATH=str(bin_dir)+os.pathsep+os.environ['PATH'],HM_TENANT_CONFIG=str(registry),HM_FACEBOOK_ACCOUNT_ROOT=str(root/'accounts'))
                    process=subprocess.Popen([sys.executable,str(Path(web.__file__)),'th'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                    output=queue.Queue()
                    threading.Thread(target=lambda:[output.put(line) for line in process.stdout],daemon=True).start()
                    try:
                        process.stdin.write(json.dumps({'action':mode,'username':'test-user','password':'must-not-be-persisted'})+'\n');process.stdin.flush()
                        first=json.loads(output.get(timeout=10));self.assertEqual(first['phase'],'WAITING_BROWSER' if mode=='browser' else 'WAITING_CODE')
                        if mode=='browser':
                            process.stdin.write(json.dumps({'action':'view','op':'snapshot','requestId':'frame-1'})+'\n');process.stdin.flush()
                            self.assertEqual(json.loads(output.get(timeout=10))['browser']['requestId'],'frame-1')
                        self.assertEqual((profile/'Cookies').read_text(),'original-session')
                        process.stdin.write(json.dumps({'action':'close'} if cancel else {'action':'finish'} if mode=='browser' else {'action':'code','oneTimeCode':'123456'})+'\n');process.stdin.flush()
                        if not cancel:self.assertEqual(json.loads(output.get(timeout=10))['phase'],'SUCCEEDED')
                        self.assertEqual(process.wait(timeout=15),0)
                        self.assertEqual((profile/'Cookies').read_text(),'original-session' if cancel else 'verified-session')
                        self.assertFalse(list((root/'accounts/test').glob('login.*')))
                        for file in root.rglob('*'):
                            if file.is_file():self.assertNotIn(b'must-not-be-persisted',file.read_bytes())
                    finally:
                        if process.poll() is None:process.terminate();process.wait(timeout=15)
                        process.stdin.close();process.stdout.close();process.stderr.close()
            self.assertEqual(len(reports),2);self.assertEqual(reports[0]['state'],'AVAILABLE')
        finally:server.shutdown();server.server_close()

    def test_restart_guard_holds_all_slots_and_login_maintenance_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=root/'tenants.json';config.write_text(json.dumps({'th':{'facebookAccount':{'key':'fb-th-01'}}}))
            with patch.dict(os.environ,{'HM_TENANT_CONFIG':str(config),'HM_SERVER_STATE_DIR':str(root/'server'),'HM_FACEBOOK_ACCOUNT_ROOT':str(root/'accounts'),'HM_CAPTURE_SLOTS':'8'}):
                handles=web.maintenance_locks();self.assertIsNotNone(handles)
                self.assertIsNone(worker.lock_file(root/'server/locks/CAPTURE-8.lock'))
                self.assertIsNone(worker.lock_file(root/'server/locks/UPLOAD-1.lock'))
                self.assertIsNone(accounts.acquire({'key':'fb-th-01'}))
                for handle in handles:handle.close()
                handle=worker.lock_file(root/'server/locks/CAPTURE-1.lock');self.assertTrue(web.busy());handle.close()
                self.assertFalse(web.busy())

    def test_verified_replacement_preserves_previous_session_and_sets_private_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'profile').mkdir();(root/'profile/Cookies').write_text('old-session')
            stage=root/'login.test';stage.mkdir();(stage/'Cookies').write_text('verified-new-session')
            web.promote(root,stage)
            self.assertEqual((root/'profile/Cookies').read_text(),'verified-new-session')
            self.assertEqual(list(root.glob('profile.previous.*/Cookies'))[0].read_text(),'old-session')
            self.assertEqual((root/'profile/.hermes-login-enabled').stat().st_mode & 0o777,0o600)

if __name__=='__main__':unittest.main()
