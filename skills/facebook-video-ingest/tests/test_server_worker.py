import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/"scripts"
sys.path.insert(0,str(SCRIPTS))
import hm_server_worker as worker
import hm_lottery_generation_worker as lottery
import hm_capture_gateway_extension as gateway

def simulate_regional_job(payload):
    import time
    root, registry, index, counts, mutex = payload
    real_sleep = time.sleep
    class Child:
        returncode = 0
        finished = False
        polled = False
        def poll(self):
            if not self.polled:
                self.polled = True
                return None
            if not self.finished:
                with mutex:
                    counts['active'] -= 1
                    counts['completed'] += 1
                self.finished = True
            return 0
    def start(*args, **kwargs):
        with mutex:
            counts['active'] += 1
            counts['peak'] = max(counts['peak'], counts['active'])
        return Child()
    spec=dict(tenant=('ph','th','vn','id')[index % 4],dispatchId=index+1,attempt=1,
              kind='CAPTURE',slot=1,taskNo=f'C-{index}',executionNo=f'E-{index}')
    for _ in range(2000):
        with patch.dict(os.environ, {'HM_TENANT_CONFIG':registry,'HM_SERVER_STATE_DIR':root,'HM_MIN_FREE_DISK_BYTES':'0'}), \
                patch.object(worker,'status'), patch.object(worker.subprocess,'Popen',side_effect=start) as process, \
                patch.object(worker.time,'sleep',side_effect=lambda _:real_sleep(0.04)):
            worker.run(spec)
            if process.called:return True
        real_sleep(0.005)
    raise RuntimeError('Simulated regional job did not obtain a shared slot')

class ServerWorkerTests(unittest.TestCase):
    def spec(self, **changes):
        return dict(dispatchId=12,attempt=1,kind="CAPTURE",slot=1,taskNo="C-12",executionNo="E-12",**changes)

    def test_eight_slots_have_independent_profiles_and_locks(self):
        profiles=set()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"HM_SERVER_STATE_DIR":temp}):
            for slot in range(1,9):
                spec=self.spec();spec["slot"]=slot
                profiles.add(worker.worker_environment(spec)["FACEBOOK_FOLLOWED_STATE_DIR"])
            self.assertEqual(len(profiles),8)
            path=Path(temp)/"slot.lock"
            first=worker.lock_file(path)
            self.assertIsNotNone(first)
            self.assertIsNone(worker.lock_file(path))
            first.close()
            worker.lock_file(path).close()

    def test_rejects_untrusted_commands_and_invalid_slot(self):
        for key,value in [("slot",9),("slot",True),("kind","SHELL"),("executionNo","../../escape")]:
            spec=self.spec();spec[key]=value
            with self.assertRaises(ValueError):worker.validate_spec(spec)

    def test_runner_keeps_exact_execution_and_separate_queue_commands(self):
        cmd=worker.command(self.spec())
        self.assertEqual(cmd[-4:],["--task-no","C-12","--execution-no","E-12"])
        spec=self.spec();spec["kind"]="UPLOAD"
        self.assertIn("--upload-only",worker.command(spec))

    def test_gateway_materializes_only_installed_runner(self):
        import shutil
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);scripts=root/"skills/facebook-video-ingest/scripts";scripts.mkdir(parents=True)
            shutil.copy(SCRIPTS/"hm_server_worker.py",scripts)
            result=gateway.prepare_capture_job_body({"hm_server_runner":self.spec(),"no_agent":False,"script":"evil.py"},home=root)
            self.assertTrue(result["no_agent"])
            self.assertEqual(result["script"],"hm_server_12_1.py")
            compile((root/"scripts"/result["script"]).read_text(),"runner","exec")
            gateway.cleanup_capture_job_script(result,home=root)
            self.assertFalse((root/"scripts"/result["script"]).exists())

    def test_low_disk_pauses_before_claim_or_process_start(self):
        import shutil
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
                "HM_SERVER_STATE_DIR":temp,"HM_INGEST_STATE_DIR":temp}), \
                patch.object(worker.shutil,"disk_usage",return_value=shutil._ntuple_diskusage(100,100,0)), \
                patch.object(worker,"status") as callback, patch.object(worker.subprocess,"Popen") as process:
            self.assertEqual(worker.run(self.spec()),0)
            process.assert_not_called()
            self.assertEqual(callback.call_args.args[1],"RETRY")
            self.assertIn("存储不足",callback.call_args.args[2])

    def test_lottery_uses_job_snapshot_and_preserves_leading_zeroes(self):
        job={"numbers":["01","09"],"issue":"20260909","lotteryName":"2D","winningSourceUrl":"https://example.com/draw"}
        self.assertIn("01 · 09",lottery.text_result(job))
        self.assertIn("20260909",lottery.text_result(job))
        with self.assertRaises(ValueError):lottery.validated_numbers({"numbers":[]})

    def registry(self, root):
        configs = {region:dict(backendUrl=f'http://backend-{region}:6200',workerToken=f'secret-{region}',
                              workerId=f'hm-server-{region}',mediaToken=f'media-{region}',
                              mediaRoot=str(root/region/'media'),r2Prefix=region.upper())
                   for region in ('ph','th','vn','id')}
        path=root/'tenants.json';path.write_text(json.dumps(configs))
        return path,configs

    def test_lottery_artifact_uses_region_root_and_reuses_identical_upload(self):
        import hashlib
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'result.png';path.write_bytes(b'generated-image')
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            for region in ('PH','VN','TH','ID'):
                uploader=Mock();client=uploader.create_client.return_value
                uploader.remote_metadata.return_value=None
                client.head_object.return_value={'ContentLength':path.stat().st_size,'Metadata':{'sha256':digest}}
                uploader.public_url.side_effect=lambda base,key:base+'/'+key
                with patch.object(lottery,'module_at',return_value=uploader),patch.dict(os.environ,{
                        'HM_R2_KEY_PREFIX':region,'CLOUDFLARE_R2_BUCKET':'doyin','CLOUDFLARE_R2_PUBLIC_BASE_URL':'https://cdn.example'}):
                    job={'taskNo':'T-1','jobNo':'J-1'}
                    expected=region+'/lottery/T-1/J-1.png'
                    self.assertEqual(lottery.upload_artifact(job,path),'https://cdn.example/'+expected)
                    self.assertEqual(client.upload_file.call_args.args[2],expected)
                    uploader.remote_metadata.return_value={'Metadata':{'sha256':digest}}
                    lottery.upload_artifact(job,path)
                    self.assertEqual(client.upload_file.call_count,1)

    def test_region_controls_callback_and_persistent_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,configs=self.registry(root)
            with patch.dict(os.environ, {'HM_TENANT_CONFIG':str(path),'HM_SERVER_STATE_DIR':str(root/'server')}):
                environments=[]
                for region in configs:
                    spec=self.spec(tenant=region);spec['backendUrl']='http://untrusted.example'
                    env=worker.worker_environment(worker.validate_spec(spec));environments.append(env)
                    self.assertEqual(env['HM_BACKEND_URL'],f'http://backend-{region}:6200')
                    self.assertEqual(env['HM_R2_KEY_PREFIX'],region.upper())
                    self.assertEqual(env['HM_WORKER_TOKEN'],'secret-'+region)
                for key in ('HM_SERVER_STATE_DIR','HM_INGEST_STATE_DIR','FACEBOOK_FOLLOWED_STATE_DIR','FACEBOOK_FOLLOWED_OUTPUT'):
                    self.assertEqual(len({env[key] for env in environments}),4)
                for spec in (self.spec(),self.spec(tenant='unknown')):
                    with self.assertRaises(ValueError):worker.validate_spec(spec)

    def test_identical_regional_job_ids_have_distinct_wrappers(self):
        import shutil
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,configs=self.registry(root)
            scripts=root/'skills/facebook-video-ingest/scripts';scripts.mkdir(parents=True)
            shutil.copy(SCRIPTS/'hm_server_worker.py',scripts)
            with patch.dict(os.environ, {'HM_TENANT_CONFIG':str(path)}):
                jobs=[gateway.prepare_capture_job_body({'hm_server_runner':self.spec(tenant=region)},home=root) for region in configs]
                self.assertEqual(len({job['script'] for job in jobs}),4)
                gateway.cleanup_capture_job_script(jobs[0],home=root)
                self.assertTrue(all((root/'scripts'/job['script']).exists() for job in jobs[1:]))

    def test_region_media_token_cannot_read_or_delete_another_region(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,configs=self.registry(root)
            for region in configs:
                media=Path(configs[region]['mediaRoot']);media.mkdir(parents=True)
                (media/'video.mp4').write_bytes(b'test-video')
            with patch.dict(os.environ, {'HM_TENANT_CONFIG':str(path)}):
                own=Path(configs['ph']['mediaRoot'])/'video.mp4'
                foreign=Path(configs['th']['mediaRoot'])/'video.mp4'
                self.assertEqual(gateway.resolve_capture_video_path(own,authorization='Bearer media-ph'),own.resolve())
                for helper in (gateway.resolve_capture_video_path,gateway.delete_capture_video_path):
                    with self.assertRaises(PermissionError):helper(foreign,authorization='Bearer media-ph')
                with self.assertRaises(PermissionError):gateway.resolve_capture_video_path(own,authorization='Bearer invalid')
                self.assertTrue(foreign.exists())

    def test_shared_pool_stays_at_eight_across_regions(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path,configs=self.registry(root)
            with patch.dict(os.environ, {'HM_TENANT_CONFIG':str(path),'HM_SERVER_STATE_DIR':str(root/'server')}):
                held=[worker.lock_file(root/'server'/'locks'/f'CAPTURE-{slot}.lock') for slot in range(1,9)]
                try:
                    with patch.object(worker,'status') as callback, patch.object(worker.subprocess,'Popen') as process:
                        # Each process inherits the same global root, irrespective of its region.
                        for region in configs:
                            with patch.dict(os.environ, {'HM_SERVER_STATE_DIR':str(root/'server')}):
                                self.assertEqual(worker.run(self.spec(tenant=region)),0)
                        process.assert_not_called()
                        self.assertEqual(callback.call_count,4)
                        self.assertTrue(all(call.args[1]=='RETRY' for call in callback.call_args_list))
                finally:
                    for handle in held:handle.close()

    @unittest.skipUnless(sys.platform != 'win32', 'Server file locks require Linux/macOS')
    def test_100_jobs_from_four_regions_share_eight_real_process_locks(self):
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        context=multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as temp, context.Manager() as manager:
            root=Path(temp);path,_=self.registry(root)
            counts=manager.dict(active=0,peak=0,completed=0);mutex=manager.Lock()
            with ProcessPoolExecutor(max_workers=16,mp_context=context) as pool:
                results=list(pool.map(simulate_regional_job,[(str(root/'server'),str(path),index,counts,mutex) for index in range(100)]))
            self.assertTrue(all(results))
            self.assertEqual(counts['completed'],100)
            self.assertEqual(counts['active'],0)
            self.assertEqual(counts['peak'],8)

if __name__=="__main__":unittest.main()
