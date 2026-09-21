import io
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
    def test_corrupt_source_refetch_and_second_failure_never_uploads(self):
        with mock.patch.object(media,'normalize',side_effect=media.CompatibilityError('BROKEN')),mock.patch.object(jobs,'refetch',return_value=self.base/'recovered.mp4') as refetch:
            with self.assertRaises(media.CompatibilityError):jobs.execute(self.job,self.args,self.pipeline,lambda:None)
        refetch.assert_called_once();self.assertEqual(self.s3.writes,0);self.assertTrue(list(self.base.rglob('*.original.mp4')))
    def test_successful_recovery_does_not_modify_title(self):
        with mock.patch.object(media,'normalize',side_effect=[media.CompatibilityError('BROKEN'),self.normalized(None)]),mock.patch.object(jobs,'refetch',return_value=self.base/'recovered.mp4'):
            result=jobs.execute(self.job,self.args,self.pipeline,lambda:None)
        self.assertTrue(result['recovered']);self.assertEqual(self.job['fileName'],'原标题.mp4')
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

if __name__=='__main__':unittest.main()
