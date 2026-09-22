import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import hm_media_capacity as capacity


def occupy(payload):
    root, counts, mutex = payload
    with patch.dict(os.environ, {'HM_MEDIA_CAPACITY_FILE':str(Path(root)/'capacity.json')}):
        with capacity.slot(Path(root)/'encoder.lock', blocking=True):
            with mutex:
                counts['active'] += 1
                counts['peak'] = max(counts['peak'], counts['active'])
            time.sleep(.03)
            with mutex: counts['active'] -= 1


class CapacityTest(unittest.TestCase):
    def test_parallel_s3_clients_do_not_share_default_sdk_session(self):
        try:
            import boto3
        except ImportError:
            self.skipTest('boto3 is not installed')
        import hm_review_storage as storage
        with patch.dict(os.environ, {'CLOUDFLARE_R2_ACCOUNT_ID':'test',
                'CLOUDFLARE_R2_ACCESS_KEY_ID':'test','CLOUDFLARE_R2_SECRET_ACCESS_KEY':'test'}), \
                patch.object(boto3, 'DEFAULT_SESSION', None), ThreadPoolExecutor(max_workers=2) as pool:
            clients = list(pool.map(lambda _: storage.client(), range(4)))
            try:
                self.assertIsNone(boto3.DEFAULT_SESSION)
                self.assertEqual(len({id(c) for c in clients}),4)
                self.assertTrue(all(c.meta.config.max_pool_connections >= 2 for c in clients))
            finally:
                for client in clients: client.close()

    def test_two_slots_across_processes_and_default_single_slot(self):
        import multiprocessing
        context=multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as temp, context.Manager() as manager:
            path=Path(temp)/'capacity.json'
            for limit in (1,2):
                path.write_text(json.dumps(dict(limit=limit)))
                counts=manager.dict(active=0,peak=0);mutex=manager.Lock()
                with ProcessPoolExecutor(max_workers=8,mp_context=context) as pool:
                    list(pool.map(occupy,[(temp,counts,mutex)]*24))
                self.assertEqual(counts['peak'],limit)
                self.assertEqual(counts['active'],0)

    def test_legacy_lock_and_downshift_drain_secondary(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'HM_MEDIA_CAPACITY_FILE':temp+'/capacity.json'}):
            path=Path(temp)/'capacity.json';path.write_text('{"limit":2}')
            lock=Path(temp)/'encoder.lock'
            legacy=capacity.try_lock(lock)
            with capacity.slot(lock) as secondary:
                self.assertIsNotNone(secondary)
                with capacity.slot(lock) as third:self.assertIsNone(third)
                legacy.close()
                path.write_text('{"limit":1}')
                with capacity.slot(lock) as new:self.assertIsNone(new)
            with capacity.slot(lock) as first:self.assertIsNotNone(first)

    def test_restore_requires_all_regions_empty_and_same_activation(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'HM_MEDIA_CAPACITY_FILE':temp+'/capacity.json'}):
            path=Path(temp)/'capacity.json';value=dict(limit=2,untilBacklogCleared=True,runId='first')
            path.write_text(json.dumps(value));regions=['ph','th','vn','id']
            pending={r:{'backlog':0} for r in regions}
            self.assertFalse(capacity.restore_if_drained(value,{'ph':{'backlog':0}},regions))
            pending['th']['backlog']=1
            self.assertFalse(capacity.restore_if_drained(value,pending,regions))
            pending['th']['backlog']=0
            path.write_text(json.dumps(dict(value,runId='new')))
            self.assertFalse(capacity.restore_if_drained(value,pending,regions))
            path.write_text(json.dumps(value))
            self.assertTrue(capacity.restore_if_drained(value,pending,regions))
            self.assertEqual(capacity.limit(),1)
            self.assertEqual(capacity.configuration()['restoreReason'],'APPROVED_BACKLOG_DRAINED')

    def test_missing_or_corrupt_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'HM_MEDIA_CAPACITY_FILE':temp+'/capacity.json'}):
            self.assertEqual(capacity.limit(),1)
            for value in ['broken','[]','{"limit":99}','{"limit":true}']:
                Path(temp+'/capacity.json').write_text(value);self.assertEqual(capacity.limit(),1)

    def test_finished_video_can_upload_while_peer_still_processing(self):
        import threading
        import facebook_video_ingest as ingest
        import hm_media_jobs as jobs
        import hm_review_storage as storage
        both=threading.Barrier(2);release=threading.Event();ready=threading.Event();finished=threading.Event()
        mutex=threading.Lock();calls=[];published=[]
        def process(*args):
            with mutex:index=len(calls);calls.append(index)
            both.wait(timeout=3)
            if index==0:
                if not release.wait(timeout=3):raise AssertionError('Upload waited for slow peer')
                finished.set()
            else:ready.set()
            return True
        def claim(*args):return {'jobNo':'fast'} if ready.is_set() and not published else None
        def upload(*args):
            self.assertFalse(finished.is_set());published.append('fast');release.set();return {'ok':True}
        with patch.object(capacity,'limit',side_effect=lambda:1 if published else 2), \
                patch.object(jobs,'process_one',side_effect=process), \
                patch.object(jobs,'restore_capacity_if_idle',return_value=False), \
                patch.object(storage,'process_one',return_value=False), \
                patch.object(ingest,'claim_upload',side_effect=claim), \
                patch.object(ingest,'process_upload_job',side_effect=upload):
            self.assertEqual(ingest.drain_parallel_compatibility(None,'backend','token','worker'),[{'ok':True}])
        self.assertEqual(len(calls),2);self.assertTrue(finished.is_set())

    def test_two_job_completions_keep_separate_journals(self):
        import threading
        from types import SimpleNamespace
        import hm_media_jobs as jobs
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{
                'HM_MEDIA_CAPACITY_FILE':temp+'/capacity.json','HM_MEDIA_JOB_LOCK':temp+'/jobs.lock',
                'HM_REVIEW_KEY_FILE':'configured','HM_TENANT_CONFIG':''}):
            Path(temp+'/capacity.json').write_text('{"limit":2}')
            pending=[dict(jobNo='M-a',executionVersion=1),dict(jobNo='M-b',executionVersion=1)]
            mutex=threading.Lock();barrier=threading.Barrier(2);acks=[]
            def api(backend,token,method,path,body,**kwargs):
                with mutex:
                    if path.endswith('/claim'):return pending.pop() if pending else None
                    if path.endswith('/check'):return {'active':True}
                    if path.endswith('/complete'):acks.append(path);return {'backupVerified':False}
                raise AssertionError(path)
            def execute(*args):barrier.wait(timeout=3);return {'sha256':'verified'}
            pipeline=SimpleNamespace(api_call=api,BackendError=RuntimeError)
            args=SimpleNamespace(state_dir=Path(temp))
            with patch.object(jobs,'execute',side_effect=execute),ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(jobs.process_one,args,'backend','token','worker',pipeline) for _ in range(2)]
                self.assertTrue(all(f.result() for f in futures))
            self.assertEqual(len(set(acks)),2)
            self.assertFalse(list((Path(temp)/'media-compat-jobs').glob('*.json')))


if __name__=='__main__':unittest.main()
