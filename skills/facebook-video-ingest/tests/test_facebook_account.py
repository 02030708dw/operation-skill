import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import hm_facebook_account as accounts
import hm_server_worker as worker
import test_server_worker as simulations

def account_job(payload):
    root,registry,index,counts,mutex=payload
    with patch.dict(os.environ,{'HM_FACEBOOK_ACCOUNT_ROOT':str(Path(root)/'accounts')}), patch.object(accounts,'verify',return_value={'state':'AVAILABLE'}):
        return simulations.simulate_regional_job(payload)

class AccountTests(unittest.TestCase):
    def test_100_jobs_share_one_account_without_parallel_browser_profiles(self):
        import multiprocessing
        ctx=multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as temp, ctx.Manager() as manager:
            registry=Path(temp)/'tenants.json'
            registry.write_text(json.dumps({region:{'backendUrl':'http://test','workerToken':'test','workerId':'test','mediaToken':'test',
                'mediaRoot':str(Path(temp)/region),'r2Prefix':region.upper(),'facebookAccount':{'key':'shared-test'}} for region in ['ph','th','vn','id']}))
            counts=manager.dict(active=0,peak=0,completed=0);mutex=manager.Lock()
            with ctx.Pool(12) as pool:
                self.assertTrue(all(pool.map(account_job,[(temp,str(registry),i,counts,mutex) for i in range(100)])))
            self.assertEqual(counts['completed'],100);self.assertEqual(counts['peak'],1)

    def test_login_verification_persists_only_state_and_enables_profile(self):
        config={'facebookAccount':{'key':'th-test'}}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'HM_FACEBOOK_ACCOUNT_ROOT':temp}), \
             patch.object(accounts.subprocess,'run',return_value=Mock(stdout='{"state":"AVAILABLE"}',returncode=0)), patch.object(accounts,'report'):
            lock=accounts.acquire(config['facebookAccount'])
            self.assertIsNone(accounts.acquire(config['facebookAccount']))
            result=accounts.verify(config,'th',{'password':'not-persisted','twoFactorSecret':'not-persisted'})
            self.assertEqual(result['state'],'AVAILABLE')
            root=accounts.account_root(config['facebookAccount'])
            self.assertNotIn('not-persisted',(root/'state.json').read_text())
            self.assertTrue((root/'profile/.hermes-login-enabled').exists())
            lock.close();accounts.acquire(config['facebookAccount']).close()

    def test_failed_manifest_is_not_reused_after_login_recovery(self):
        import facebook_video_ingest as ingest
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'download.json'
            payload={'status':'partial','sources':[{'videos':[{'status':'download-failed','errorCode':'FACEBOOK_ACCESS_REQUIRED'}]}]}
            path.write_text(json.dumps(payload))
            self.assertIsNone(ingest.reusable_download_result(path))
            self.assertEqual(ingest.manifest_error_code(payload),'FACEBOOK_ACCESS_REQUIRED')
            payload['sources'][0]['videos'].append({'status':'downloaded'})
            self.assertEqual(ingest.manifest_error_code(payload),'FACEBOOK_ITEM_FAILURE')

    def test_busy_account_does_not_take_a_capture_slot(self):
        config={'backendUrl':'http://test','workerToken':'test','workerId':'hm-server-th','mediaToken':'test',
                'mediaRoot':'/tmp/not-used','r2Prefix':'TH','facebookAccount':{'key':'th-test'}}
        spec={'tenant':'th','kind':'CAPTURE','slot':1,'dispatchId':1,'attempt':1,'taskNo':'C-1','executionNo':'E-1'}
        with patch.object(worker,'tenant_config',return_value=config), patch.object(accounts,'acquire',return_value=None), \
             patch.object(worker,'status'), patch.object(worker,'lock_file') as slots, \
             patch.object(worker.os,'environ',dict(os.environ)):
            self.assertEqual(worker.run(spec),0)
            slots.assert_not_called()

    def test_container_recreation_removes_only_stale_browser_locks(self):
        with tempfile.TemporaryDirectory() as temp:
            profile=Path(temp)/'profile';profile.mkdir()
            proc=Path(temp)/'proc';proc.mkdir()
            (profile/'Cookies').write_text('persisted-session')
            (profile/'SingletonLock').symlink_to('old-container-123')
            (profile/'SingletonSocket').symlink_to('/tmp/old-browser-socket')
            accounts.recover_profile(profile,proc)
            self.assertFalse((profile/'SingletonLock').is_symlink())
            self.assertFalse((profile/'SingletonSocket').is_symlink())
            self.assertEqual((profile/'Cookies').read_text(),'persisted-session')

    def test_invalid_or_missing_account_config(self):
        self.assertIsNone(accounts.account_config({}))
        with self.assertRaises(ValueError): accounts.account_config({'facebookAccount':{'key':'../th'}})
