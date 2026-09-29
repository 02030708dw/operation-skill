import io
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import hm_media_jobs as jobs
import hm_media_compat as media

class Body(io.BytesIO):
    def iter_chunks(self,chunk_size):
        while True:
            chunk=self.read(chunk_size)
            if not chunk:break
            yield chunk

class Storage:
    def __init__(self):self.values={'review/TH/V-A/original.mp4':(b'original','old',{})};self.writes=0
    def head_object(self,Bucket,Key,**kw):
        data,etag,metadata=self.values[Key];return {'ContentLength':len(data),'ETag':etag,'Metadata':metadata}
    def get_object(self,Bucket,Key,IfMatch,**kw):
        data,etag,_=self.values[Key];assert IfMatch==etag;return {'Body':Body(data)}
    def copy_object(self,Bucket,Key,CopySource,Metadata,**kw):
        self.values[Key]=(self.values[CopySource["Key"]][0],"backup",Metadata)
    def put_object(self,Bucket,Key,Body,Metadata,**kw):
        assert Key not in self.values;assert kw['IfNoneMatch']=='*';self.values[Key]=(Body.read(),'new',Metadata);self.writes+=1

class MediaJobsTest(unittest.TestCase):
    def setUp(self):
        # These tests use tiny fake media and must not depend on host free space.
        disk=mock.patch.object(jobs.shutil,'disk_usage',return_value=SimpleNamespace(free=100*1024**3))
        disk.start();self.addCleanup(disk.stop)
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.s3=Storage()
        self.args=SimpleNamespace(state_dir=self.base);self.pipeline=SimpleNamespace(resolve_local_delete_path=lambda p,_:Path(p))
        self.job={'jobNo':'M-test','subject':'V-A','region':'TH','bucket':'test','ruleVersion':1,'executionVersion':1,'key':'review/TH/V-A/original.mp4','keyVersion':'v1','sourceETag':'old','targetKey':'review/TH/V-A/new.mp4','targetKeyVersion':'v1','fileSize':8,'sha256':__import__('hashlib').sha256(b'original').hexdigest(),'fileName':'原标题.mp4','videos':[]}
        self.patches=[mock.patch.dict(os.environ,{'HM_MEDIA_BACKUP_ROOT':str(self.base),'CLOUDFLARE_R2_BUCKET':'test'}),mock.patch.object(jobs.storage,'configuration',return_value={'activeVersion':'v1'}),mock.patch.object(jobs.storage,'encryption',return_value={}),mock.patch.object(jobs.storage,'client',return_value=self.s3),mock.patch.object(jobs.storage,'head',side_effect=lambda s,b,k,e:self.s3.head_object(b,k) if k in self.s3.values else None)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def normalized(self,path,data=b'converted'):
        target=self.base/'output.mp4';target.write_bytes(data);return {'path':str(target),'sha256':media.sha256(target),'fileSize':len(data),'converted':data!=b'original','durationSeconds':1,'version':1}
    def test_conversion_retains_original_and_verifies_uploaded_bytes(self):
        with mock.patch.object(media,'normalize',side_effect=self.normalized):result=jobs.execute(self.job,self.args,self.pipeline,lambda:None)
        self.assertEqual(Path(result['backupPath']).read_bytes(),b'original');self.assertTrue(result['decoded']);self.assertEqual(result['targetKey'],self.job['targetKey']);self.assertEqual(self.s3.writes,1)
        self.assertNotIn('title',result);self.assertEqual(self.job['fileName'],'原标题.mp4')
    def test_compatible_reuses_object_without_upload(self):
        with mock.patch.object(media,'normalize',side_effect=lambda p:self.normalized(p,b'original')):result=jobs.execute(self.job,self.args,self.pipeline,lambda:None)
        self.assertEqual(result['targetKey'],self.job['key']);self.assertEqual(self.s3.writes,0)
    def test_inspection_routes_conversion_without_upload(self):
        with mock.patch.object(media,'normalize',side_effect=media.ConversionRequired('MEDIA_COMPAT_CONVERSION_REQUIRED')) as normalize:
            with self.assertRaises(media.ConversionRequired):jobs.execute(self.job,self.args,self.pipeline,lambda:None,'INSPECT')
        self.assertFalse(self.s3.writes)
        self.assertEqual(normalize.call_args.kwargs,{'allow_conversion':False})
    def test_inspection_releases_lease_to_conversion_lane(self):
        calls=[]
        def api(_backend,_token,_method,path,body,**_kwargs):
            calls.append((path,body))
            if path.endswith('/claim'):return {'jobNo':'M-route','executionVersion':4}
            if path.endswith('/convert'):return {'acknowledged':True,'routed':True}
            raise AssertionError(path)
        pipeline=SimpleNamespace(api_call=api,BackendError=RuntimeError)
        with mock.patch.dict(os.environ,{'HM_REVIEW_KEY_FILE':'configured','HM_TENANT_CONFIG':'',
                'HM_MEDIA_JOB_LOCK':str(self.base/'jobs.lock')}), \
                mock.patch.object(jobs.capacity,'slot',return_value=contextlib.nullcontext(object())), \
                mock.patch.object(jobs.storage,'lease',return_value=contextlib.nullcontext(lambda:None)), \
                mock.patch.object(jobs,'execute',side_effect=media.ConversionRequired('MEDIA_COMPAT_CONVERSION_REQUIRED')):
            self.assertTrue(jobs.process_one(self.args,'backend','token','worker',pipeline,'INSPECT'))
        self.assertEqual(calls[0][1]['lane'],'INSPECT')
        self.assertEqual(calls[-1],('/api/internal/capture/media-compat/M-route/convert',
                                   {'workerId':'worker','executionVersion':4}))
        self.assertFalse(list((self.base/'media-compat-jobs').glob('*.json')))
    def test_corrupt_source_refetch_and_second_failure_never_uploads(self):
        with mock.patch.object(media,'normalize',side_effect=media.CompatibilityError('BROKEN')),mock.patch.object(jobs,'refetch',return_value=self.base/'recovered.mp4') as refetch:
            with self.assertRaises(media.CompatibilityError):jobs.execute(self.job,self.args,self.pipeline,lambda:None)
        refetch.assert_called_once();self.assertEqual(self.s3.writes,0);self.assertTrue(list(self.base.rglob('*.original.mp4')))
    def test_successful_recovery_does_not_modify_title(self):
        with mock.patch.object(media,'normalize',side_effect=[media.CompatibilityError('BROKEN'),self.normalized(None)]),mock.patch.object(jobs,'refetch',return_value=self.base/'recovered.mp4'):
            result=jobs.execute(self.job,self.args,self.pipeline,lambda:None)
        self.assertTrue(result['recovered']);self.assertEqual(self.job['fileName'],'原标题.mp4')
    def test_windowed_conversion_failure_never_redownloads_from_platform(self):
        self.job['windowedMode']=True
        with mock.patch.object(media,'normalize',side_effect=media.CompatibilityError('BROKEN')),mock.patch.object(jobs,'refetch') as refetch:
            with self.assertRaisesRegex(media.CompatibilityError,'BROKEN'):
                jobs.execute(self.job,self.args,self.pipeline,lambda:None)
        refetch.assert_not_called()
        self.assertEqual(self.s3.writes,0)
    def test_changed_source_and_cross_region_are_rejected(self):
        self.job['sourceETag']='wrong'
        with self.assertRaises(jobs.JobFailure):jobs.execute(self.job,self.args,self.pipeline,lambda:None)
        self.job['key']='review/PH/V-A/original.mp4'
        with self.assertRaises(jobs.JobFailure):jobs.execute(self.job,self.args,self.pipeline,lambda:None)
    def test_corrupt_target_download_cannot_produce_success(self):
        original=self.s3.get_object
        def altered(**kw):return {'Body':Body(b'tampered')} if kw['Key']==self.job['targetKey'] else original(**kw)
        with mock.patch.object(media,'normalize',side_effect=self.normalized),mock.patch.object(self.s3,'get_object',side_effect=altered):
            with self.assertRaisesRegex(jobs.JobFailure,'TARGET_HASH_MISMATCH'):jobs.execute(self.job,self.args,self.pipeline,lambda:None)
    def test_interruption_preserves_source(self):
        with mock.patch.object(media,'normalize',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):jobs.execute(self.job,self.args,self.pipeline,lambda:None)
        self.assertTrue(list(self.base.rglob('*.original.mp4')));self.assertEqual(self.s3.writes,0)

    def test_failed_job_cleans_local_copy_only_after_bucket_source_is_verified(self):
        cache=self.base/'TH'/'M-test';cache.mkdir(parents=True)
        local=cache/'source.mp4';local.write_bytes(b'original')
        self.assertTrue(jobs.cleanup_failed_cache(self.job))
        self.assertFalse(local.exists())
        local.write_bytes(b'original')
        self.s3.values.pop(self.job['key'])
        self.assertFalse(jobs.cleanup_failed_cache(self.job))
        self.assertTrue(local.exists())

    def test_interrupted_cache_sweep_checks_lease_before_cleanup(self):
        cache=self.base/'TH'/'M-test';cache.mkdir(parents=True)
        job=dict(self.job,workerId='worker')
        source=cache/'source.json';source.write_text(__import__('json').dumps(job))
        local=cache/'source.mp4';local.write_bytes(b'original')
        old=__import__('time').time()-7200
        os.utime(source,(old,old))
        calls=[]
        def status(path,body):
            calls.append(path);return {'active':False}
        jobs.cleanup_stale_cache(self.base,'worker',status)
        marker=self.base/'media-cache-sweep-worker.stamp'
        os.utime(marker,(old,old))
        jobs.cleanup_stale_cache(self.base,'worker',status)
        self.assertEqual(['M-test/check'],calls)
        self.assertFalse(local.exists())

class RecoveryIdentityTest(unittest.TestCase):
    def run_recovery(self, returned_id='123', duration=1):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);registry=root/'tenants.json';registry.write_text(json.dumps({'th':{}}))
            def download(platform,item,output,credentials):
                path=output/'recovered.mp4';path.write_bytes(b'recovered')
                return dict(id=returned_id,status='downloaded',path=str(path))
            capture=SimpleNamespace(attempt=lambda operation,*args:operation({}),download_item=download,classify=lambda error:'NETWORK_ERROR')
            before={'format':{'duration':'1'},'streams':[{'codec_type':'video','start_time':'0','duration':'1'}]}
            after={'format':{'duration':str(duration)},'streams':[{'codec_type':'video','start_time':'0','duration':str(duration)}]}
            job={'region':'TH','videos':[dict(platform='Facebook',platform_video_id='123',canonical_url='https://www.facebook.com/watch/?v=123')]}
            with mock.patch.dict(sys.modules,{'hm_public_capture':capture}),mock.patch.dict(os.environ,{'HM_TENANT_CONFIG':str(registry)}),mock.patch.object(media,'probe',side_effect=[before,after]):
                return jobs.refetch(job,root,root/'original.mp4',lambda:None)
    def test_wrong_platform_video_is_rejected(self):
        with self.assertRaisesRegex(jobs.JobFailure,'RECOVERY_IDENTITY_MISMATCH'):self.run_recovery('999')
    def test_truncated_replacement_is_rejected(self):
        with self.assertRaisesRegex(media.CompatibilityError,'DURATION_MISMATCH'):self.run_recovery(duration=2)
    def test_same_identity_and_duration_can_be_recovered(self):
        self.assertEqual(self.run_recovery().name,'recovered.mp4')

if __name__=='__main__':unittest.main()
