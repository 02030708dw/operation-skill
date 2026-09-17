import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import hm_public_capture as public
import hm_server_worker as worker

class PublicCaptureTests(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{"HM_CAPTURE_TENANT":"vn"});env.start();self.addCleanup(env.stop)

    def test_account_callbacks_use_current_region(self):
        for region in ('ph','th','vn','id'):
            with patch.dict(os.environ,{'HM_CAPTURE_TENANT':region}),patch.object(public.google,'download_result') as report:
                public.account_outcome({'googleAccount':{'key':'google-'+region}},'YouTube',None)
                self.assertEqual(region,report.call_args.args[1])
        with patch.dict(os.environ,{'HM_CAPTURE_TENANT':'unknown'}):
            with self.assertRaises(public.ingest.PipelineError):public.current_tenant()

    def test_download_and_recovered_receipt_include_preview_integrity(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'video.mp4';path.write_bytes(b'preview fixture')
            for status in ('downloaded','skipped'):
                record=public.video_record({'id':'video','url':'https://www.youtube.com/watch?v=video','path':str(path),'status':status})
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),record['sha256'])
                with patch.object(public.ingest,'api_call') as request:
                    public.ingest.record_video('http://localhost','token','worker','execution',record,download_status='DOWNLOADED',upload_status='PENDING')
                    self.assertEqual(record['sha256'],request.call_args.args[4]['fileSha256'])

    def test_public_never_reads_account_or_inherited_credentials(self):
        for platform in ('Facebook','YouTube'):
            attempts=[]
            with patch.object(public,'authenticated_session') as auth:
                self.assertEqual('ok',public.attempt(lambda credentials: 'ok' if not credentials else 'bad',{},platform,attempts,'DOWNLOAD'))
                auth.assert_not_called()
            env=public.clean_environment(dict.fromkeys(public.AUTH_ENV,'secret'))
            self.assertFalse(set(public.AUTH_ENV)&set(env))
            self.assertEqual('PUBLIC',attempts[0]['mode'])

    def test_login_failure_gets_only_one_supplement(self):
        @contextlib.contextmanager
        def session(*_):yield {'cookiefile':'private'}
        for final in (None,'LOGIN_REQUIRED','NETWORK_ERROR'):
            attempts=[];calls=[]
            def operation(credentials):
                calls.append(bool(credentials))
                if not credentials or final:raise public.Failure(final if credentials else 'LOGIN_REQUIRED')
                return 'ok'
            with patch.object(public,'authenticated_session',session),patch.object(public,'account_outcome'):
                if final:
                    with self.assertRaises(public.Failure):public.attempt(operation,{},'YouTube',attempts,'DOWNLOAD')
                else:self.assertEqual('ok',public.attempt(operation,{},'YouTube',attempts,'DOWNLOAD'))
            self.assertEqual([False,True],calls)

    def test_authenticated_discovery_does_not_claim_download_validation(self):
        @contextlib.contextmanager
        def session(*_):yield {'cookiefile':'private'}
        operation=Mock(side_effect=[public.Failure('LOGIN_REQUIRED'),[{'id':'video'}]])
        with patch.object(public,'authenticated_session',session),patch.object(public,'account_outcome') as state:
            self.assertEqual([{'id':'video'}],public.attempt(operation,{},'YouTube',[],'DISCOVERY'))
            state.assert_not_called()

    def test_rate_limit_and_verification_never_switch_accounts(self):
        for code in ('RATE_LIMITED','VERIFICATION_REQUIRED','ACCOUNT_SUSPENDED','NETWORK_ERROR'):
            with patch.object(public,'authenticated_session') as auth:
                with self.assertRaises(public.Failure):public.attempt(Mock(side_effect=public.Failure(code)),{},'Facebook',[],'DOWNLOAD')
                auth.assert_not_called()

    def test_security_challenge_during_saved_session_check_stops_supplement(self):
        config={'facebookAccount':{'key':'fb-vn'}};lease=Mock()
        with patch.object(public.facebook,'read_state',return_value={'state':'AVAILABLE'}),patch.object(public.facebook,'acquire_capture',return_value=lease),patch.object(public.facebook,'prepare_capture',return_value={'state':'VERIFICATION_REQUIRED','reasonCode':'FACEBOOK_VERIFICATION_REQUIRED'}):
            with self.assertRaises(public.Failure) as caught:
                with public.authenticated_session(config,'Facebook'):self.fail('must not download')
            self.assertEqual('VERIFICATION_REQUIRED',caught.exception.code);lease.close.assert_called_once()

    def test_missing_invalid_or_busy_account_is_finite(self):
        with self.assertRaisesRegex(public.Failure,'未配置'):
            with public.authenticated_session({},'Facebook'):pass
        config={'facebookAccount':{'key':'fb-vn'}}
        with patch.object(public.facebook,'read_state',return_value={'state':'LOGIN_REQUIRED'}),patch.object(public.facebook,'acquire_capture') as lock:
            with self.assertRaises(public.Failure):
                with public.authenticated_session(config,'Facebook'):pass
            lock.assert_not_called()
        with patch.object(public.facebook,'read_state',return_value={'state':'AVAILABLE'}),patch.object(public.facebook,'acquire_capture',return_value=None):
            with self.assertRaisesRegex(public.Failure,'维护'):
                with public.authenticated_session(config,'Facebook'):pass

    def test_mixed_batch_continues_after_login_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'media';report=root/'run.json'
            entries=[{'id':str(i),'url':'https://www.facebook.com/reel/'+str(i)} for i in range(3)]
            def download(platform,item,output,credentials):
                if item['id']=='1':raise public.Failure('LOGIN_REQUIRED')
                p=output/(item['id']+'.mp4');p.write_bytes(b'fixture')
                return dict(item,status='downloaded',path=str(p))
            with patch.object(public,'discover',return_value=entries),patch.object(public,'download_item',side_effect=download) as dl,patch.object(public.time,'sleep'):
                r=public.run_download('Facebook','https://www.facebook.com/page',3,out,report,{})
                self.assertEqual({'downloaded':2,'skipped':0,'failed':1,'filtered-duration':0,'unattempted':0},r['counts'])
                self.assertEqual('PASSED',r['publicDownload']['status'])
                r=public.run_download('Facebook','https://www.facebook.com/page',3,out,root/'again.json',{})
                self.assertEqual(2,r['counts']['skipped']);self.assertEqual(4,dl.call_count)

    def test_limit_stops_and_records_unattempted_without_account_mutation(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'HM_SERVER_STATE_DIR':tmp}):
            root=Path(tmp);entries=[{'id':str(i),'url':'https://www.facebook.com/reel/'+str(i)} for i in range(3)]
            with patch.object(public,'discover',return_value=entries),patch.object(public,'download_item',side_effect=public.Failure('RATE_LIMITED')),patch.object(public,'account_outcome') as state:
                r=public.run_download('Facebook','https://www.facebook.com/page',3,root/'out',root/'run.json',{})
                self.assertEqual(2,r['counts']['unattempted']);self.assertEqual('COOLDOWN',r['publicDownload']['status']);state.assert_not_called()
                self.assertTrue((root/'public-cooldown/Facebook.json').exists())

    def test_unknown_discovery_count_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(public,'discover',side_effect=public.Failure('LOGIN_REQUIRED')):
            root=Path(tmp);r=public.run_download('Facebook','https://www.facebook.com/page',10,root/'out',root/'run.json',{})
            self.assertIsNone(r['counts']['unattempted']);self.assertEqual('ACCOUNT_NOT_CONFIGURED',r['errorCode'])

    def test_callback_failure_reuses_receipt_and_reconciles_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def download(platform,item,output,credentials):
                p=output/'clip.mp4';p.write_bytes(b'fixture');return dict(item,status='downloaded',path=str(p))
            with patch.object(public,'download_item',side_effect=download) as dl:
                with self.assertRaisesRegex(RuntimeError,'callback'):
                    public.run_download('YouTube','https://www.youtube.com/shorts/0ixRDhyAsMY',1,root/'out',root/'run.json',{},Mock(side_effect=RuntimeError('callback')))
                r=public.run_download('YouTube','https://www.youtube.com/shorts/0ixRDhyAsMY',1,root/'out',root/'run.json',{})
                self.assertEqual(1,dl.call_count);self.assertEqual(1,r['counts']['downloaded'])

    def test_worker_anon_does_not_acquire_account_and_policy_is_regional(self):
        config={'capturePolicy':'PUBLIC_FIRST','facebookAccount':{'key':'fb-vn'}}
        spec=dict(dispatchId=1,attempt=1,kind='CAPTURE',slot=1,taskNo='C-X',executionNo='E-X',tenant='vn',platform='Facebook',capturePolicy='PUBLIC_FIRST')
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'HM_SERVER_STATE_DIR':tmp,'HM_MIN_FREE_DISK_BYTES':'0'}),patch.object(worker,'tenant_config',return_value=config),patch.object(worker,'status'),patch.object(worker,'worker_environment',return_value=dict(os.environ,TMPDIR=tmp)),patch.object(public.facebook,'acquire_capture') as account,patch.object(worker.subprocess,'Popen',return_value=Mock(poll=Mock(return_value=0),returncode=0)):
            worker.run(spec);account.assert_not_called()
        with patch.object(worker,'tenant_config',return_value=config):
            for region in ('ph','th','vn','id'):
                spec['tenant']=region
                self.assertEqual(region,worker.validate_spec(spec)['tenant'])
            spec['tenant']='vn';spec['capturePolicy']='ACCOUNT_REQUIRED'
            with self.assertRaises(ValueError):worker.validate_spec(spec)

    def test_existing_youtube_archive_is_respected_without_network(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(public,'download_item') as download:
            root=Path(tmp);out=root/'YouTube';out.mkdir();(out/'.download-archive.txt').write_text('youtube 0ixRDhyAsMY\n')
            r=public.run_download('YouTube','https://www.youtube.com/shorts/0ixRDhyAsMY',1,out,root/'run.json',{})
            self.assertEqual(1,r['counts']['skipped']);download.assert_not_called()

    def test_facebook_direct_link_does_not_need_feed_discovery(self):
        self.assertIsNotNone(public.single_source('Facebook','https://www.facebook.com/reel/123456'))
        self.assertIsNone(public.single_source('Facebook','https://www.facebook.com/example/videos'))

if __name__=='__main__':unittest.main()
