import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import hm_capture_pipeline_worker as pipeline
import hm_pipeline_discovery as discovery
import hm_server_worker as runner


class PipelineTests(unittest.TestCase):
    def test_round_robin_regions_and_platforms_borrow_empty_shares(self):
        rotation=pipeline.Rotation(pipeline.REGIONS)
        first=[]
        for _ in range(8):
            first.append(next(rotation.candidates()))
        self.assertEqual([r for r,_ in first],list(pipeline.REGIONS)*2)
        self.assertEqual([p for _,p in first[:4]],['Facebook']*4)
        self.assertEqual([p for _,p in first[4:]],['YouTube']*4)

    def test_reserved_check_slots_cannot_be_taken_by_downloads(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'HM_CAPTURE_SLOTS':'8'}):
            root=Path(temp);held=[]
            try:
                for _ in range(6):
                    slot,lock=pipeline.stage_lock(root,'DOWNLOAD');self.assertIn(slot,range(3,9));held.append(lock)
                self.assertEqual(pipeline.stage_lock(root,'DOWNLOAD'),(None,None))
                for expected in (1,2):
                    slot,lock=pipeline.stage_lock(root,'CHECK');self.assertEqual(expected,slot);held.append(lock)
                self.assertEqual(pipeline.stage_lock(root,'CHECK'),(None,None))
            finally:
                for lock in held:lock.close()

    def test_truncated_and_stalled_discovery_is_not_success(self):
        rows=[{'id':str(i)} for i in range(50)]
        result=discovery.page_result(rows,exhausted=False,validated=True,cursor={'offset':50},frontier=set())
        self.assertEqual('INCOMPLETE',result['outcome']);self.assertFalse(result['complete'])
        result=discovery.page_result([],exhausted=True,validated=False,cursor={},frontier=set())
        self.assertFalse(result['complete'])

    def test_validated_empty_source_is_a_successful_check(self):
        result=discovery.page_result([],exhausted=True,validated=True,cursor={},frontier=set())
        self.assertEqual('SUCCESS',result['outcome'])

    def test_even_three_pinned_known_items_cannot_prove_complete_coverage(self):
        rows=[{'id':str(i)} for i in range(6)]
        def check(known):
            return discovery.page_result(rows,exhausted=False,validated=True,cursor={},frontier=set(known))['complete']
        self.assertFalse(check(['0']))
        self.assertFalse(check(['0','2','4']))
        self.assertFalse(check(['3','4','5']))

    def test_facebook_continuation_uses_provider_cursor_not_a_capped_prefix(self):
        captured=[]
        def execute(argv,**kwargs):
            captured.append(json.loads(argv[argv.index('--pipeline-cursor')+1]))
            from types import SimpleNamespace
            return SimpleNamespace(stdout='HM_PUBLIC_DISCOVERY '+json.dumps({'validated':True,'entries':[{'id':'5001','url':'https://www.facebook.com/watch/?v=5001'}],
                'sourceExhausted':False,'cursor':{'candidate':0,'provider':{'key':'123:all_videos','after':'next-5002'}}}))
        with patch.object(discovery.subprocess,'run',side_effect=execute):
            result=discovery.facebook('https://www.facebook.com/example',{'facebook':{'candidate':0,'provider':{'key':'123:all_videos','after':'next-5001'}}},set(),{})
        self.assertEqual('next-5001',captured[0]['provider']['after'])
        self.assertEqual('5001',result['entries'][0]['id']);self.assertFalse(result['complete'])
        self.assertEqual('next-5002',result['cursor']['facebook']['provider']['after'])

    def test_durable_receipt_replayed_after_lost_ack_without_rerunning_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'1.json';job={'jobNo':'P-test','leaseVersion':1,'tenant':'ph'}
            pipeline.atomic_json(path,job)
            receipt={'leaseVersion':1,'outcome':'SUCCESS','complete':True,'validated':True}
            pipeline.atomic_json(pipeline.result_file(path),receipt)
            calls=[]
            def api(config,url,body):
                calls.append((url,dict(body)))
                if url.endswith('/complete') and len(calls)==2:raise OSError('lost acknowledgement')
                return {'accepted':True}
            with patch.object(runner,'tenant_config',return_value={}),patch.object(pipeline,'request',side_effect=api),patch.object(pipeline.time,'sleep'),patch.object(pipeline.subprocess,'Popen') as launch:
                self.assertEqual(0,pipeline.run_job(path));launch.assert_not_called()
            self.assertEqual(calls[-1][1],calls[-2][1])

    def test_partial_public_discovery_is_retained_when_more_requires_login(self):
        import hm_public_capture as public
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'1.json'
            pipeline.atomic_json(path,{'jobNo':'P-partial','leaseVersion':1,'tenant':'ph','stage':'CHECK','platform':'Facebook'})
            partial=public.Failure('LOGIN_REQUIRED');partial.entries=[{'id':'123','url':'https://www.facebook.com/reel/123'}]
            with patch.object(runner,'tenant_config',return_value={}),patch.object(discovery,'check',side_effect=partial),patch.object(public,'authenticated_session',side_effect=public.Failure('ACCOUNT_NOT_CONFIGURED')):
                pipeline.execute_stage(path)
            result=json.loads(pipeline.result_file(path).read_text())
            self.assertEqual('LOGIN_REQUIRED',result['outcome']);self.assertEqual(partial.entries,result['entries'])
            self.assertNotEqual(True,result.get('complete'));self.assertEqual('ACCOUNT_NOT_CONFIGURED',result['errorCode'])

    def test_old_verified_file_is_copied_without_network_download(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'old.mp4';source.write_bytes(b'old-owner-file')
            path=root/'1.json';job={'jobNo':'P-reuse','leaseVersion':1,'tenant':'ph','stage':'DOWNLOAD','platform':'Facebook','subjectKey':'fixture',
                'entry':{'id':'123','url':'https://www.facebook.com/reel/123'},'reuseCandidates':[{'localPath':str(source),'fileSha256':pipeline.digest(source),'fileName':'old.mp4'}]}
            pipeline.atomic_json(path,job)
            with patch.object(runner,'tenant_config',return_value={'mediaRoot':str(root)}),patch('hm_public_capture.download_item',side_effect=AssertionError('must reuse verified file')):
                pipeline.execute_stage(path)
            result=json.loads(pipeline.result_file(path).read_text());self.assertEqual('SUCCESS',result['outcome'])
            copied=Path(result['video']['localPath']);self.assertNotEqual(source,copied);self.assertTrue(result['video']['reused'])
            source.unlink();self.assertTrue(copied.is_file())

    def test_provider_retry_after_survives_failure_wrapping(self):
        import hm_public_capture as public
        from email.utils import formatdate
        error=Exception('429 Too many requests');error.headers={'Retry-After':'7200'}
        with self.assertRaises(public.Failure) as raised:
            public.attempt(lambda _: (_ for _ in ()).throw(error),{},'Facebook',[],'DISCOVERY')
        self.assertEqual(7200,public.retry_after(raised.exception))
        error.headers={'Retry-After':formatdate(public.time.time()+4000,usegmt=True)}
        self.assertGreaterEqual(public.retry_after(error),3998)

    def test_stage_completion_receipt_does_not_include_regional_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'1.json';job={'jobNo':'P-test','leaseVersion':1,'tenant':'ph','stage':'CHECK','platform':'Facebook'}
            pipeline.atomic_json(path,job)
            config={'workerToken':'secret-token','workerId':'worker-ph'}
            with patch.object(runner,'tenant_config',return_value=config),patch.object(discovery,'check',return_value={'outcome':'SUCCESS','complete':True,'validated':True,'entries':[]}):
                pipeline.execute_stage(path)
            text=pipeline.result_file(path).read_text()
            self.assertNotIn('secret-token',text);self.assertTrue(json.loads(text)['complete'])


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
class PipelineMediaTests(unittest.TestCase):
    def test_media_validation_precedes_delivery_and_retries_reuse_canonical_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source.webm'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=120x200:rate=24',
                '-t','0.6','-c:v','libvpx-vp9','-threads','1',str(source)],check=True,capture_output=True)
            sha=pipeline.digest(source)
            job={'jobNo':'P-media','leaseVersion':1,'tenant':'ph','stage':'MEDIA','platform':'Facebook',
                 'subjectKey':'fixture','taskIds':[10,20],
                 'download':{'localPath':str(source),'fileSha256':sha,'fileName':'source.webm','fileSize':source.stat().st_size}}
            path=root/'job.json';pipeline.atomic_json(path,job)
            environment={'HM_MEDIA_COMPAT_ENABLED':'1','HM_SERVER_STATE_DIR':str(root/'state'),'HM_MEDIA_LOCK_DIR':str(root/'locks')}
            with patch.object(runner,'tenant_config',return_value={'mediaRoot':str(root)}),patch.dict(os.environ,environment):
                pipeline.execute_stage(path)
                result=json.loads(pipeline.result_file(path).read_text())
                self.assertEqual('SUCCESS',result['outcome']);video=result['video']
                self.assertTrue(video['mediaCompatibility']['converted']);self.assertEqual(sha,pipeline.digest(source))
                self.assertEqual(2,len(video['deliveryPaths']))
                for delivered in video['deliveryPaths'].values():self.assertEqual(video['fileSha256'],pipeline.digest(delivered))
                # Review cleanup of one ownership must not remove another or the canonical file.
                Path(video['deliveryPaths']['10']).unlink()
                self.assertTrue(Path(video['deliveryPaths']['20']).is_file())
                job['taskIds']=[30];pipeline.atomic_json(path,job)
                with patch('hm_media_compat.normalize',side_effect=AssertionError('verified cache should be reused')):
                    pipeline.execute_stage(path)
                repeated=json.loads(pipeline.result_file(path).read_text())
                self.assertEqual('SUCCESS',repeated['outcome']);self.assertTrue(Path(repeated['video']['deliveryPaths']['30']).is_file())

    def test_corrupt_file_cannot_enter_review(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'invalid.mp4';source.write_bytes(b'not a video')
            job={'jobNo':'P-bad-media','leaseVersion':1,'tenant':'ph','stage':'MEDIA','platform':'Facebook',
                'subjectKey':'invalid','taskIds':[10],
                'download':{'localPath':str(source),'fileSha256':pipeline.digest(source),'fileName':'invalid.mp4'}}
            path=root/'job.json';pipeline.atomic_json(path,job)
            with patch.object(runner,'tenant_config',return_value={'mediaRoot':str(root)}),patch.dict(os.environ,{'HM_MEDIA_COMPAT_ENABLED':'1','HM_SERVER_STATE_DIR':str(root/'state')}):
                pipeline.execute_stage(path)
            result=json.loads(pipeline.result_file(path).read_text())
            self.assertEqual('FAILED',result['outcome']);self.assertNotIn('video',result)
            self.assertFalse((root/'executions').exists())


if __name__=='__main__':unittest.main()
