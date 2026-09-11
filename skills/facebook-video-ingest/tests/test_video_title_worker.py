import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import hm_video_title_worker as title
import hm_server_worker as server
import facebook_video_ingest as ingest
class VideoTitleWorkerTest(unittest.TestCase):
    def test_explicit_object_key_preserves_unicode(self):
        key='PH/Sports/202609/11/U-1/a1/สมชาย-การแข่งขัน.mp4'
        self.assertEqual(key,ingest.planned_upload_key({'objectKey':key,'fileName':'wrong.mp4'}))
    def test_durable_result_replayed_without_recapture(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'T-1.pending';title.atomic(p,{'jobNo':'T-1','executionVersion':2,'title':'caption'})
            with patch.object(title,'api',return_value={'acknowledged':True}) as api:title.deliver(p)
            self.assertEqual('/T-1/complete',api.call_args.args[0]);self.assertFalse(p.exists());self.assertTrue(p.with_suffix('.ack').exists())
    def test_outage_retains_result_and_stale_lease_cannot_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'T-1.pending';title.atomic(p,{'jobNo':'T-1','executionVersion':2})
            with patch.object(title,'api',side_effect=OSError('offline')):
                with self.assertRaises(OSError):title.deliver(p)
            self.assertTrue(p.exists())
            with patch.object(title,'api',side_effect=urllib.error.HTTPError('test',409,'stale',{},None)):title.deliver(p)
            self.assertTrue(p.with_suffix('.stale').exists())
    def test_review_upload_accepts_only_its_authoritative_unicode_filename(self):
        import hm_review_storage as storage
        from types import SimpleNamespace
        job={'region':'PH','videoNo':'V-1','jobNo':'R-1','executionVersion':2,'fileName':'博主-สมชาย.mp4','objectKey':'review/PH/V-1/R-1/a2/博主-สมชาย.mp4','bucket':'doyin','keyVersion':'v1','kind':'UPLOAD','fileSize':3,'fileSha256':'sha'}
        with patch.dict(os.environ,{'CLOUDFLARE_R2_BUCKET':'doyin'}),patch.object(storage,'configuration',return_value={}),patch.object(storage,'encryption',return_value={}),patch.object(storage,'client'),patch.object(storage,'head',return_value={'ContentLength':3,'Metadata':{'hm-sha256':'sha'}}):
            storage.execute(job,SimpleNamespace(),None,lambda:None)
            with self.assertRaises(storage.StorageFailure):storage.execute(dict(job,fileName='wrong.mp4'),SimpleNamespace(),None,lambda:None)
    def test_title_runner_is_metadata_only(self):
        with patch.dict(os.environ,{},clear=True):
            spec=server.validate_spec({'dispatchId':1,'attempt':1,'slot':1,'kind':'TITLE'})
            command=server.command(spec);self.assertTrue(command[-1].endswith('hm_video_title_worker.py'));self.assertNotIn('--execute',command)
