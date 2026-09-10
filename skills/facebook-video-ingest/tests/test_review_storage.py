import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import hm_review_storage as storage

class ReviewStorageTest(unittest.TestCase):
    def test_region_and_key_boundary(self):
        storage.require_review_key('TH','review/TH/V1/a1/video.mp4')
        for key in ['review/PH/V1/video.mp4','TH/video.mp4','review/TH/../PH/video.mp4','review/TH//a.mp4']:
            with self.assertRaises(storage.StorageFailure):storage.require_review_key('TH',key)
    def test_no_key_version_fallback(self):
        import base64
        c={'keys':{'v1':base64.b64encode(bytes(32)).decode()}}
        self.assertEqual(storage.encryption(c,'v1')['SSECustomerAlgorithm'],'AES256')
        self.assertIn('CopySourceSSECustomerKey',storage.encryption(c,'v1',True))
        with self.assertRaises(storage.StorageFailure):storage.encryption(c,'v2')
    def test_checksum_and_size_required_for_idempotent_copy(self):
        job={'fileSize':3,'fileSha256':'sha'}
        storage.verify({'ContentLength':3,'Metadata':{'hm-sha256':'sha'}},job)
        for head in [None,{'ContentLength':3,'Metadata':{}},{'ContentLength':4,'Metadata':{'hm-sha256':'sha'}}]:
            with self.assertRaises(storage.StorageFailure):storage.verify(head,job)
    def test_callback_failure_keeps_journal_for_recovery_without_repeating_upload(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'HM_REVIEW_KEY_FILE':'configured'}):
            args=SimpleNamespace(state_dir=Path(tmp));calls=[]
            job={'jobNo':'R-1','executionVersion':1}
            def api(*args,**kwargs):
                path=args[3];calls.append(path)
                if path.endswith('/claim'):return job
                if path.endswith('/check'):return {'active':True}
                raise RuntimeError('callback disconnected')
            p=SimpleNamespace(api_call=api)
            with patch.object(storage,'execute') as execute:
                with self.assertRaises(RuntimeError):storage.process_one(args,'backend','token','worker',p)
                self.assertEqual(execute.call_count,1)
            self.assertEqual(len(list((Path(tmp)/'review-storage').glob('*.json'))),1)
            def recovered(*args,**kwargs):return None if args[3].endswith('/claim') else {'acknowledged':True}
            p.api_call=recovered
            with patch.object(storage,'execute') as execute:
                self.assertFalse(storage.process_one(args,'backend','token','worker',p));execute.assert_not_called()
            self.assertEqual(list((Path(tmp)/'review-storage').glob('*.json')),[])
    def test_failure_journal_does_not_leak_sdk_headers(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'HM_REVIEW_KEY_FILE':'configured'}):
            calls=[]
            def api(*args,**kwargs):
                if args[3].endswith('/claim'):return {'jobNo':'R-2','executionVersion':1}
                if args[3].endswith('/check'):return {'active':True}
                calls.append(args[4]);return {'acknowledged':True}
            with patch.object(storage,'execute',side_effect=RuntimeError('SENSITIVE_SSE_KEY')):
                storage.process_one(SimpleNamespace(state_dir=Path(tmp)),'backend','token','worker',SimpleNamespace(api_call=api))
            self.assertEqual(calls[0]['errorCode'],'STORAGE_IO_FAILED');self.assertNotIn('SENSITIVE',json.dumps(calls))

if __name__=='__main__':unittest.main()
