import io
import argparse
import contextlib
import fcntl
import os
from pathlib import Path
import sys
import tempfile
import unittest
import json
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import hm_media_evacuate as evacuation

class Body(io.BytesIO):
    def iter_chunks(self,chunk_size):
        while True:
            part=self.read(chunk_size)
            if not part:break
            yield part
class S3:
    def __init__(self):self.data=None
    def upload_file(self,path,bucket,key,**kw):self.data=Path(path).read_bytes()
    def get_object(self,**kw):return {'Body':Body(self.data)}

class EvacuationTests(unittest.TestCase):
    def test_command_uses_real_worker_http_api_and_regional_storage_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            key=Path(tmp)/'key.json';key.write_text(json.dumps({'region':'PH','publicReadDenied':True,'activeVersion':'v1'}));key.chmod(0o600)
            config=dict(backendUrl='https://backend.test',workerToken='private-test-token',workerId='hm-server-ph',r2Prefix='PH',reviewKeyFile=str(key))
            def evaluate(args,region,call):
                self.assertEqual(evacuation.storage.configuration(region)['activeVersion'],'v1')
                self.assertEqual(call('inventory',{}),{'running':0})
            response=contextlib.nullcontext(io.BytesIO(b'{"data":{"running":0}}'))
            with patch.dict(os.environ,{},clear=False),patch.object(sys,'argv',['evacuate','--region','ph']),patch.object(evacuation.runner,'tenant_config',return_value=config),patch.object(evacuation,'evacuate',side_effect=evaluate),patch.object(evacuation.runner.urllib.request,'urlopen',return_value=response) as request:
                evacuation.main()
                req=request.call_args.args[0]
                self.assertEqual(req.full_url,'https://backend.test/api/internal/capture/local-media/inventory')
                self.assertEqual(json.loads(req.data),{'workerId':'hm-server-ph'})
    def fixture(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        path=Path(tmp.name)/'original.mp4';path.write_bytes(b'only reliable source');s3=S3()
        patches=[patch.dict(os.environ,{'CLOUDFLARE_R2_BUCKET':'test'}),patch.object(evacuation.storage,'configuration',return_value={'activeVersion':'v1'}),patch.object(evacuation.storage,'encryption',return_value={}),patch.object(evacuation.storage,'client',return_value=s3),patch.object(evacuation.storage,'head',side_effect=lambda *a:None if s3.data is None else {'ETag':'etag','ContentLength':len(s3.data),'Metadata':{'hm-sha256':evacuation.media.sha256(path)}}),patch.object(evacuation,'opened',return_value=set())]
        for p in patches:p.start();self.addCleanup(p.stop)
        return path,s3
    def test_unlink_follows_verified_upload_and_committed_reference(self):
        path,s3=self.fixture();called=[]
        def confirm(action,body):
            self.assertTrue(path.exists());self.assertEqual(s3.data,path.read_bytes());called.append(body);return {'archived':True}
        self.assertGreater(evacuation.archive(path,'PH',confirm),0)
        self.assertFalse(path.exists());self.assertTrue(called[0]['objectKey'].startswith('review/PH/legacy-retained/'))
    def test_interrupted_r2_upload_preserves_source(self):
        path,s3=self.fixture()
        with patch.object(s3,'upload_file',side_effect=OSError('R2 unavailable')),self.assertRaises(OSError):evacuation.archive(path,'PH',lambda *_:None)
        self.assertTrue(path.exists())
    def test_failed_callback_and_content_verification_never_delete_source(self):
        for corrupt in [True,False]:
            path,s3=self.fixture()
            with patch.object(evacuation.jobs,'remote_sha256',return_value='bad' if corrupt else evacuation.media.sha256(path)),self.assertRaises(RuntimeError):
                evacuation.archive(path,'TH',lambda *_:{'archived':False})
            self.assertTrue(path.exists())
    def test_unknown_extensions_remain_in_capacity_acceptance_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'download.mp4.part').write_bytes(b'partial');(root/'unknown.bin').write_bytes(b'unverified');(root/'download.json').write_text('{}')
            args=argparse.Namespace(region='ph',apply=False,limit=50)
            with contextlib.redirect_stdout(io.StringIO()):
                report=evacuation.evacuate(args,'PH',lambda *_:{'running':0,'blockedPaths':[]},[root])
            self.assertEqual(report['candidates'],1);self.assertEqual(report['remainingFiles'],2)
            self.assertTrue((root/'unknown.bin').exists());self.assertTrue((root/'download.mp4.part').exists())
    def test_maintenance_holds_all_idle_lanes_until_operation_finishes(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'HM_SERVER_STATE_DIR':tmp,'HM_CAPTURE_SLOTS':'2'}):
            with evacuation.maintenance(True):
                for name in ['CAPTURE-1','CAPTURE-2','UPLOAD-1','DELETE-1','GENERATION-1']:
                    with (Path(tmp)/'locks'/(name+'.lock')).open('a') as handle,self.assertRaises(BlockingIOError):fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with (Path(tmp)/'locks/CAPTURE-1.lock').open('a') as handle:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)

if __name__=='__main__':unittest.main()
