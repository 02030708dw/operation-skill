import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path[:0]=[str(SCRIPTS),str(SCRIPTS.parents[1]/'facebook-video-ingest/scripts')]
import hm_google_account as google
import hm_youtube_ingest as adapter
import hm_server_worker as worker

class GoogleTests(unittest.TestCase):
    def test_login_and_capture_exclude_each_other(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'HM_GOOGLE_ACCOUNT_ROOT':tmp}):
            account={'key':'vn-test'}
            lease=google.acquire_capture(account)
            self.assertIsNotNone(lease)
            self.assertIsNone(google.acquire(account))
            self.assertIsNone(google.acquire_capture(account))
            lease.close()
            maintenance=google.acquire(account)
            self.assertIsNotNone(maintenance)
            self.assertIsNone(google.acquire_capture(account))
            maintenance.close()

    def test_login_does_not_claim_download_passed(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'HM_GOOGLE_ACCOUNT_ROOT':tmp}),patch.object(google,'report'):
            config={'googleAccount':{'key':'vn-test'}}
            google.save_result(config,'vn',dict(state='LOGGED_IN',loginStatus='LOGGED_IN',downloadStatus='NOT_TESTED',maskedAccount='te***@example.com'))
            self.assertEqual('NOT_TESTED',google.read_state(config['googleAccount'])['downloadStatus'])
            google.download_result(config,'vn')
            self.assertEqual('PASSED',google.read_state(config['googleAccount'])['downloadStatus'])
            google.download_result(config,'vn','GOOGLE_LOGIN_REQUIRED')
            self.assertEqual('LOGIN_REQUIRED',google.read_state(config['googleAccount'])['loginStatus'])
            self.assertEqual('FAILED',google.read_state(config['googleAccount'])['downloadStatus'])

    def test_archive_receipt_replays_missing_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'video.mp4';path.write_bytes(b'fixture')
            item=dict(id='0ixRDhyAsMY',path=str(path),title='Test',status='skipped',duration=25.1,upload_date='20230625')
            video=adapter.video_record(item)
            self.assertEqual('downloaded',video['status'])
            self.assertEqual(25,video['durationSeconds'])
            self.assertEqual(path.stat().st_size,video['fileSize'])
            self.assertEqual(video,adapter.video_record(item))
            path.unlink();self.assertIsNone(adapter.video_record(item))

    def test_finished_download_is_replayed_without_launching_downloader(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'E-TEST';folder.mkdir();video=root/'clip.mp4';video.write_bytes(b'fixture')
            (folder/'youtube-download.json').write_text(json.dumps({'finished_at':'done','counts':{'downloaded':1,'skipped':0,'failed':0,'unattempted':0},'results':[{'id':'0ixRDhyAsMY','status':'downloaded','path':str(video)}]}))
            registry=root/'tenants.json';registry.write_text(json.dumps({'vn':{'googleAccount':{'key':'vn'}}}))
            args=SimpleNamespace(backend='https://backend.example',worker_token='token',worker_id='worker',state_dir=root,heartbeat_seconds=30,count=10)
            job={'executionId':'E-TEST','sourceUrl':'https://www.youtube.com/shorts/0ixRDhyAsMY'}
            with patch.dict(os.environ,{'HM_CAPTURE_TENANT':'vn','HM_TENANT_CONFIG':str(registry),'FACEBOOK_FOLLOWED_OUTPUT':str(root)}),patch.object(adapter.ingest,'HeartbeatPump'),patch.object(adapter.ingest,'heartbeat'),patch.object(adapter.ingest,'record_video') as record,patch.object(adapter.ingest,'complete') as complete,patch.object(adapter.accounts,'download_result'),patch.object(adapter.subprocess,'Popen') as popen:
                for _ in range(2):self.assertEqual(0,adapter.execute(args,job)[0])
                popen.assert_not_called();self.assertEqual(2,record.call_count)
                self.assertEqual('DOWNLOADED',record.call_args.kwargs['download_status'])
                self.assertEqual('COMPLETED',complete.call_args.args[4])

    def test_worker_requires_regional_youtube_configuration(self):
        spec=dict(dispatchId=1,attempt=1,kind='CAPTURE',slot=1,taskNo='C-TEST',executionNo='E-TEST',platform='YouTube',tenant='ph')
        with self.assertRaises(ValueError):worker.validate_spec(spec)
        for region in ('ph','th','vn','id'):
            spec['tenant']=region
            with patch.object(worker,'tenant_config',return_value={'googleAccount':{'key':region}}):
                self.assertEqual('YouTube',worker.validate_spec(spec)['platform'])

    def test_account_restriction_survives_partial_success(self):
        self.assertEqual('GOOGLE_LOGIN_REQUIRED',adapter.report_code({'results':[{'id':'a','status':'downloaded'},{'id':'b','status':'failed','kind':'authentication_required'}]}))

if __name__=='__main__':unittest.main()
