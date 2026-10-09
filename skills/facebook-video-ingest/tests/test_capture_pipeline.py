import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
import socket
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import hm_capture_pipeline_worker as pipeline
import hm_pipeline_discovery as discovery
import hm_server_worker as runner


class PipelineTests(unittest.TestCase):
    def test_failed_x_import_records_public_discovery_reason_without_stopping_next_link(self):
        import hm_x_capture as x
        import hm_public_capture as public
        with tempfile.TemporaryDirectory() as temp,patch.object(runner,'tenant_config',return_value={}):
            job=Path(temp)/'job.json'
            job.write_text(json.dumps(dict(stage='X_IMPORT',leaseVersion=3,sourceUrl='https://x.com/creator/status/123',windowedMode=True)))
            with patch.object(x,'discover',side_effect=public.Failure('X_VIDEO_UNAVAILABLE')):
                pipeline._execute_stage(job)
            result=json.loads(pipeline.result_file(job).read_text())
            self.assertEqual('FAILED',result['outcome'])
            self.assertEqual('X_VIDEO_UNAVAILABLE',result['errorCode'])
            self.assertEqual([dict(stage='DISCOVERY',mode='PUBLIC',result='FAILED',reasonCode='X_VIDEO_UNAVAILABLE')],result['attempts'])
            with patch.object(x,'discover',return_value=[{'id':'456'}]):pipeline._execute_stage(job)
            self.assertEqual('SUCCESS',json.loads(pipeline.result_file(job).read_text())['outcome'])

    def test_title_limit_counts_java_utf16_units_without_splitting_emoji(self):
        self.assertEqual('a'*299,pipeline.backend_title('a'*299+'😀'))
        self.assertEqual('a'*298+'😀',pipeline.backend_title('a'*298+'😀'+'b'))

    def test_stage_browser_socket_fits_even_with_long_inherited_job_directory(self):
        roots=[]
        def stage(_):
            root=Path(os.environ['TMPDIR']);roots.append(root)
            self.assertEqual(0o700,root.stat().st_mode & 0o777)
            socket_path=root/'org.chromium.Chromium.abcdef'/'SingletonSocket'
            socket_path.parent.mkdir()
            with socket.socket(socket.AF_UNIX) as connection:
                connection.bind(str(socket_path))
        inherited='/tmp/'+'long-pipeline-job-directory/'*8
        with patch.dict(os.environ,{'TMPDIR':inherited}),patch.object(pipeline,'_execute_stage',side_effect=stage):
            pipeline.execute_stage('first.json');pipeline.execute_stage('second.json')
            self.assertEqual(inherited,os.environ['TMPDIR'])
        self.assertNotEqual(roots[0],roots[1])
        self.assertTrue(all(not root.exists() for root in roots))

    def test_failed_stage_cleans_private_browser_temp_and_restores_environment(self):
        roots=[]
        def stage(_):
            roots.append(Path(os.environ['TMPDIR']))
            raise RuntimeError('stage failed')
        with patch.dict(os.environ,{'TMPDIR':'/tmp/original'}),patch.object(pipeline,'_execute_stage',side_effect=stage):
            with self.assertRaisesRegex(RuntimeError,'stage failed'):pipeline.execute_stage('failed.json')
            self.assertEqual('/tmp/original',os.environ['TMPDIR'])
        self.assertFalse(roots[0].exists())

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

    def test_continuous_blogger_checks_do_not_starve_imports_with_one_free_discovery_slot(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'HM_CAPTURE_SLOTS':'8'}):
            root=Path(temp);_,first=pipeline.stage_lock(root,'CHECK');_,occupied=pipeline.stage_lock(root,'CHECK');first.close()
            turn=pipeline.DiscoveryTurn();started=[]
            try:
                for _ in range(8):
                    running=[]
                    for stage in turn.stages():
                        if stage not in ('CHECK','X_IMPORT'):continue
                        _,lock=pipeline.stage_lock(root,stage)
                        if lock:
                            running.append(lock);started.append(stage);turn.started(stage)
                    for lock in running:lock.close()
                self.assertEqual(['CHECK','X_IMPORT']*4,started)
            finally:occupied.close()

    def test_truncated_and_stalled_discovery_is_not_success(self):
        rows=[{'id':str(i)} for i in range(50)]
        result=discovery.page_result(rows,exhausted=False,validated=True,cursor={'offset':50},frontier=set())
        self.assertEqual('INCOMPLETE',result['outcome']);self.assertFalse(result['complete'])
        result=discovery.page_result([],exhausted=True,validated=False,cursor={},frontier=set())
        self.assertFalse(result['complete'])

    def test_validated_empty_source_is_a_successful_check(self):
        result=discovery.page_result([],exhausted=True,validated=True,cursor={},frontier=set())
        self.assertEqual('SUCCESS',result['outcome'])

    def test_latest_ten_uses_only_validated_first_feed_page(self):
        import hm_public_capture as public
        rows=[{'id':str(i),'url':'https://www.youtube.com/watch?v=abcdefghijk'} for i in range(50)]
        page={'outcome':'INCOMPLETE','complete':False,'validated':True,'entries':rows}
        job={'platform':'YouTube','sourceUrl':'https://www.youtube.com/@fixture','latestTen':True}
        with patch.object(public,'single_source',return_value=None),patch.object(discovery,'youtube',return_value=page) as youtube:
            result=discovery.check(job,{})
        self.assertEqual('SUCCESS',result['outcome']);self.assertEqual(10,len(result['entries']))
        self.assertEqual('VALIDATED_LATEST_TEN',result['evidence'])
        self.assertEqual({},youtube.call_args.args[1])
        self.assertEqual(10,youtube.call_args.args[-1])
        with patch.object(public,'single_source',return_value=None),patch.object(discovery,'youtube',return_value=dict(page,entries=rows[:9])):
            with self.assertRaises(public.Failure):discovery.check(job,{})

    def test_windowed_review_upload_returns_verified_bucket_receipt_without_local_path(self):
        import hm_review_storage as storage
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'video.mp4';path.write_bytes(b'video-source')
            video={'localPath':str(path),'fileSize':path.stat().st_size,'fileSha256':pipeline.digest(path)}
            class Bucket:
                def upload_file(self,*args,**kwargs):pass
            with patch.dict(os.environ,{'CLOUDFLARE_R2_BUCKET':'fixture-bucket'}),\
                patch.object(storage,'configuration',return_value={'activeVersion':'v1'}),\
                patch.object(storage,'client',return_value=Bucket()),\
                patch.object(storage,'encryption',return_value={}),\
                patch.object(storage,'head',side_effect=[None,{'ContentLength':path.stat().st_size,'Metadata':{'hm-sha256':video['fileSha256']}}]),\
                patch('hm_media_compat.probe',return_value={'streams':[{'codec_type':'video','codec_name':'h264','codec_tag_string':'avc1','width':16,'height':16,'pix_fmt':'yuv420p','profile':'Main','level':42,'avg_frame_rate':'30/1'}],'format':{'format_name':'mp4','duration':'1'}}):
                receipt=pipeline.store_review_original(video,'ph','key1')
            self.assertIsNone(receipt['localPath']);self.assertEqual('fixture-bucket',receipt['reviewBucket'])
            self.assertTrue(receipt['reviewObjectKey'].startswith('review/PH/pipeline/key1/'))

    def test_windowed_download_keeps_verified_video_in_completion_receipt(self):
        import hm_public_capture as public
        import hm_review_storage as storage
        for cached in (False, True):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as temp:
                root=Path(temp);directory=root/'pipeline'/'video-key';directory.mkdir(parents=True)
                source=directory/'original.mp4';source.write_bytes(b'original-media')
                spec=root/'jobs'/'1.json'
                job={'jobNo':'P-download','leaseVersion':1,'tenant':'ph','stage':'DOWNLOAD',
                     'platform':'Facebook','subjectKey':'video-key','windowedMode':True,
                     'entry':{'id':'123','url':'https://www.facebook.com/reel/123'}}
                pipeline.atomic_json(spec,job)
                sha=pipeline.digest(source)
                stored=[]
                class Bucket:
                    def upload_file(self, path, bucket, key, **kwargs):
                        stored.append((Path(path).read_bytes(),key))
                def head(*args):
                    return {'ContentLength':len(b'original-media'),'Metadata':{'hm-sha256':sha}} if stored or cached else None
                if cached:
                    pipeline.atomic_json(directory/'download.json',dict(localPath=None,
                        platformVideoId='123',fileSize=source.stat().st_size,fileSha256=sha,title='123',
                        reviewObjectKey='review/PH/pipeline/video-key/source.mp4',reviewBucket='fixture',reviewKeyVersion='v1'))
                with patch.dict(os.environ,{'CLOUDFLARE_R2_BUCKET':'fixture'}),\
                     patch.object(runner,'tenant_config',return_value={'mediaRoot':str(root)}),\
                     patch.object(public,'attempt',side_effect=lambda action,*_:action({})),\
                     patch.object(public,'download_item',return_value={'path':str(source),'title':'123','duration':1}) as download,\
                     patch.object(storage,'configuration',return_value={'activeVersion':'v1'}),\
                     patch.object(storage,'client',return_value=Bucket()),\
                     patch.object(storage,'encryption',return_value={}),\
                     patch.object(storage,'head',side_effect=head),\
                     patch('hm_media_compat.probe',return_value={'streams':[{'codec_type':'video','codec_name':'h264',
                         'codec_tag_string':'avc1','width':16,'height':16,'pix_fmt':'yuv420p','profile':'Main','level':42,
                         'avg_frame_rate':'30/1'}],'format':{'format_name':'mp4','duration':'1'}}):
                    pipeline.execute_stage(spec)
                    receipt=json.loads(pipeline.result_file(spec).read_text())
                    self.assertEqual('SUCCESS',receipt['outcome'])
                    self.assertIsInstance(receipt['video'],dict)
                    self.assertEqual(sha,receipt['video']['fileSha256'])
                    self.assertIsNone(receipt['video']['localPath'])
                    self.assertEqual(receipt['video'],json.loads((directory/'download.json').read_text()))
                    if cached:download.assert_not_called()
                    else:self.assertEqual(b'original-media',stored[0][0])
                    received=[]
                    with patch.object(pipeline,'request',side_effect=lambda config,url,body:received.append((url,body)) or {'accepted':True}),\
                         patch.object(pipeline.subprocess,'Popen') as launch:
                        self.assertEqual(0,pipeline.run_job(spec))
                        launch.assert_not_called()
                    self.assertEqual(receipt,received[-1][1])
                    self.assertFalse(source.exists())

    def test_legacy_download_is_imported_without_network_or_early_file_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);directory=root/'pipeline'/'old-key';directory.mkdir(parents=True)
            source=directory/'original.mp4';source.write_bytes(b'legacy-source')
            job={'jobNo':'P-adopt','leaseVersion':1,'tenant':'ph','stage':'MEDIA','platform':'Facebook',
                'subjectKey':'old-key','legacyAdopt':True,'windowedMode':True,
                'download':{'localPath':str(source),'fileSize':source.stat().st_size,'fileSha256':pipeline.digest(source)}}
            spec=root/'job.json';pipeline.atomic_json(spec,job)
            receipt=dict(job['download'],localPath=None,reviewObjectKey='review/PH/pipeline/old-key/source.mp4')
            with patch.object(runner,'tenant_config',return_value={'mediaRoot':str(root)}),\
                patch.object(pipeline,'store_review_original',return_value=receipt) as upload,\
                patch('hm_public_capture.download_item',side_effect=AssertionError('must not download')):
                pipeline.execute_stage(spec)
            self.assertEqual('SUCCESS',json.loads(pipeline.result_file(spec).read_text())['outcome'])
            upload.assert_called_once();self.assertTrue(source.exists())
            pipeline.result_file(spec).unlink()
            source.write_bytes(b'changed')
            with patch.object(runner,'tenant_config',return_value={'mediaRoot':str(root)}),\
                patch.object(pipeline,'verify_review_receipt',return_value=None),\
                patch.object(pipeline,'store_review_original',side_effect=AssertionError('must not upload')):
                pipeline.execute_stage(spec)
            self.assertEqual('SUCCESS',json.loads(pipeline.result_file(spec).read_text())['outcome'])
            (directory/'media.json').unlink();pipeline.result_file(spec).unlink()
            with patch.object(runner,'tenant_config',return_value={'mediaRoot':str(root)}),\
                patch.object(pipeline,'store_review_original',side_effect=AssertionError('must not upload')):
                pipeline.execute_stage(spec)
            failed=json.loads(pipeline.result_file(spec).read_text())
            self.assertEqual('FAILED',failed['outcome']);self.assertEqual('LEGACY_FILE_CHANGED',failed['errorCode'])
            self.assertTrue(source.exists())
            source.unlink();pipeline.result_file(spec).unlink()
            alternate=directory/'originals'/'moved.mp4';alternate.parent.mkdir()
            alternate.write_bytes(b'legacy-source')
            with patch.object(runner,'tenant_config',return_value={'mediaRoot':str(root)}),\
                patch.object(pipeline,'store_review_original',return_value=receipt) as upload:
                pipeline.execute_stage(spec)
            self.assertEqual('SUCCESS',json.loads(pipeline.result_file(spec).read_text())['outcome'])
            self.assertEqual(str(alternate.resolve()),upload.call_args.args[0]['localPath'])
            self.assertTrue(alternate.exists())

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

    def test_windowed_ack_removes_only_its_verified_local_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);job_dir=root/'jobs'/'P-clean';job_dir.mkdir(parents=True)
            media_dir=root/'media'/'pipeline'/'video-key';media_dir.mkdir(parents=True)
            (media_dir/'source.mp4').write_bytes(b'temporary')
            path=job_dir/'1.json'
            pipeline.atomic_json(path,{'jobNo':'P-clean','leaseVersion':1,'tenant':'ph',
                'stage':'DOWNLOAD','subjectKey':'video-key','windowedMode':True})
            pipeline.atomic_json(pipeline.result_file(path),{'leaseVersion':1,'outcome':'SUCCESS'})
            with patch.object(runner,'tenant_config',return_value={'mediaRoot':str(root/'media')}):
                with patch.object(pipeline,'request',return_value={'accepted':True}):
                    with patch.object(pipeline.subprocess,'Popen') as launch:
                        self.assertEqual(0,pipeline.run_job(path))
                        launch.assert_not_called()
            self.assertFalse(media_dir.exists())
            self.assertFalse(job_dir.exists())

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
                 'download':{'localPath':str(source),'fileSha256':sha,'fileName':'source.webm','fileSize':source.stat().st_size,
                             'title':'a'*290+'😀'*10}}
            path=root/'job.json';pipeline.atomic_json(path,job)
            environment={'HM_MEDIA_COMPAT_ENABLED':'1','HM_SERVER_STATE_DIR':str(root/'state'),'HM_MEDIA_LOCK_DIR':str(root/'locks')}
            with patch.object(runner,'tenant_config',return_value={'mediaRoot':str(root)}),patch.dict(os.environ,environment):
                pipeline.execute_stage(path)
                result=json.loads(pipeline.result_file(path).read_text())
                self.assertEqual('SUCCESS',result['outcome']);video=result['video']
                self.assertEqual(300,len(video['title'].encode('utf-16-le'))//2)
                self.assertEqual(video['title'],json.loads((root/'pipeline'/'fixture'/'media.json').read_text())['title'])
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
