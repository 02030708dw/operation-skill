import json
import errno
import multiprocessing as mp
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import socket
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import hm_media_workspace as ws

def applicant(path,tenant,ready,release):
    with patch.object(ws,'root',return_value=Path(path)):
        try:
            with ws.job(tenant,'one',2*1024**3):
                ready.put((tenant,'admitted'));release.wait(8)
        except ws.WorkspaceFailure as error:ready.put((tenant,str(error)))

class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.p=patch.object(ws,'root',return_value=self.root);self.p.start()
    def tearDown(self):self.p.stop();self.tmp.cleanup()
    def test_four_regions_share_one_atomic_budget(self):
        ctx=mp.get_context('fork');ready=ctx.Queue();release=ctx.Event()
        jobs=[ctx.Process(target=applicant,args=(str(self.root),r,ready,release)) for r in ['ph','th','vn','id']]
        for job in jobs:job.start()
        outcomes=[ready.get(timeout=10)[1] for _ in jobs]
        release.set()
        for job in jobs:job.join(10);self.assertEqual(job.exitcode,0)
        self.assertEqual(outcomes.count('admitted'),2);self.assertEqual(outcomes.count('MEDIA_WORKSPACE_BUSY'),2)
        self.assertEqual(json.loads((self.root/'.leases.json').read_text()),{})
    def test_r2_interruption_preserves_the_only_valid_original(self):
        with ws.job('ph','protected',1024**2) as path:
            source=path/'source.mp4';source.write_bytes(b'unique original')
            ws.protect_source(source,'a'*64)
        self.assertTrue(source.exists())
        # Re-entry must work even in the same long-lived process after lease release.
        with ws.job('ph','protected',1024**2):ws.verified()
        self.assertFalse(path.exists())
    def test_verified_files_and_partial_downloads_are_cleaned_on_exception(self):
        for confirmed in [False,True]:
            with self.assertRaises(ValueError):
                with ws.job('th','crash',1024**2) as path:
                    (path/'fragment.part').write_bytes(b'partial')
                    if confirmed:ws.verified()
                    raise ValueError('process failure')
            self.assertFalse(path.exists())
    def test_single_item_larger_than_budget_fails_permanently(self):
        with self.assertRaises(ws.WorkspaceFailure) as error:
            with ws.job('vn','large',ws.CAPACITY):pass
        self.assertFalse(error.exception.retryable)
    def test_metadata_growth_reservation_and_oversized_transport(self):
        with ws.job('ph','growing',512*1024**2):
            ws.reserve_input(300*1024**2)
            record=json.loads((self.root/'.leases.json').read_text())[ws.key_for('ph','growing')]
            self.assertEqual(record['reserved'],964*1024**2)
            with self.assertRaises(ws.WorkspaceFailure) as error:ws.download_progress({'downloaded_bytes':ws.CAPACITY})
            self.assertFalse(error.exception.retryable)
    def test_dead_process_partial_is_reclaimed_but_protected_source_is_kept(self):
        (self.root/'work').mkdir();folder=self.root/'work'/'dead';folder.mkdir();(folder/'part').write_bytes(b'partial')
        (self.root/'.leases.json').write_text(json.dumps({'dead':dict(pid=os.getpid(),identity='old',reserved=1024,verified=False)}))
        with ws.job('ph','next',1024):pass
        self.assertFalse(folder.exists())
    def test_disk_full_queues_without_creating_media(self):
        fs=type('FS',(),dict(f_bavail=0,f_frsize=4096))()
        with patch.object(ws.os,'statvfs',return_value=fs),self.assertRaises(ws.WorkspaceFailure) as error:
            with ws.job('id','full',1024):pass
        self.assertTrue(error.exception.retryable)
    def test_pid_reuse_does_not_allow_deleting_an_unverified_source(self):
        (self.root/'work').mkdir();folder=self.root/'work'/'stale';folder.mkdir();(folder/'source').write_bytes(b'only copy')
        (folder/'.source-protected.json').write_text('{}')
        (self.root/'.leases.json').write_text(json.dumps({'stale':dict(pid=os.getpid(),identity='different-start-time',reserved=1024,verified=False)}))
        with ws.job('ph','next',1024):pass
        self.assertTrue((folder/'source').exists())
    def test_plain_filesystem_is_rejected_even_with_a_budget_json(self):
        self.p.stop();(self.root/'.budget.json').write_text(json.dumps({'capacityBytes':ws.CAPACITY}))
        with patch.dict(os.environ,{'HM_EPHEMERAL_MEDIA_ROOT':str(self.root)}),self.assertRaises(ws.WorkspaceFailure):ws.root()
        self.p.start()
    def test_transport_and_transcode_disk_full_queue_without_leaking_output(self):
        with patch.dict(os.environ,{'HM_EPHEMERAL_MEDIA_ROOT':str(self.root)}):
            for error in [OSError(errno.ENOSPC,'private path'),subprocess.CalledProcessError(1,['ffmpeg'],stderr=b'private path: No space left on device')]:
                failure=ws.storage_failure(error)
                self.assertTrue(failure.retryable);self.assertEqual(str(failure),'MEDIA_WORKSPACE_BUSY')
            with patch.dict(os.environ,{'HM_JOB_MEDIA_ROOT':str(self.root)}),patch.object(ws,'bytes_used',return_value=ws.CAPACITY):
                self.assertFalse(ws.storage_failure(OSError(errno.ENOSPC,'full')).retryable)
    def test_browser_lock_links_allow_parallel_admission_without_following_targets(self):
        with ws.job('ph','browser',1024**2) as media:
            temporary=Path(ws.temporary_root());profile=temporary/'profile';profile.mkdir()
            (profile/'cache').write_bytes(b'cache')
            for name in ('SingletonLock','SingletonSocket','SingletonCookie'):
                (profile/name).symlink_to('/outside-the-workspace')
            key=ws.key_for('ph','browser');lease=json.loads((self.root/'.leases.json').read_text())[key]
            self.assertGreater(ws.lease_bytes(self.root,key,lease),0)
            with ws.job('th','parallel',1024**2):self.assertTrue(temporary.exists())
            self.assertEqual(str(temporary),os.environ['HM_JOB_TEMP_ROOT'])
            self.assertEqual(str(media),os.environ['HM_JOB_MEDIA_ROOT'])
        self.assertFalse(temporary.exists())
        self.assertEqual(json.loads((self.root/'.leases.json').read_text()),{})
    def test_media_links_and_unknown_browser_links_remain_rejected(self):
        with ws.job('ph','links',1024) as media:
            link=media/'SingletonLock';link.symlink_to('/outside')
            with self.assertRaises(ws.WorkspaceFailure):ws.bytes_used(media)
            link.unlink()
            temporary=Path(ws.temporary_root());(temporary/'arbitrary').symlink_to('/outside')
            with self.assertRaises(ws.WorkspaceFailure):ws.bytes_used(temporary,True)
    def test_short_browser_root_fits_actual_unix_socket_and_is_cleaned(self):
        with tempfile.TemporaryDirectory(prefix='hmb-',dir='/tmp') as short,patch.object(ws,'root',return_value=Path(short)):
            with ws.job('id','socket',1024):
                with tempfile.TemporaryDirectory(prefix='hp-',dir=ws.temporary_root()) as temporary:
                    path=Path(temporary)/'org.chromium.Chromium.abcdef'/'SingletonSocket';path.parent.mkdir()
                    self.assertLess(len(str(path).encode()),108)
                    with socket.socket(socket.AF_UNIX) as connection:connection.bind(str(path))
            self.assertEqual(list((Path(short)/'t').iterdir()),[])
    def test_dead_browser_temp_is_reclaimed_without_deleting_protected_media(self):
        (self.root/'work').mkdir();folder=self.root/'work'/'dead';folder.mkdir()
        (folder/'source.mp4').write_bytes(b'only copy');(folder/'.source-protected.json').write_text('{}')
        temporary=self.root/'t'/'b-dead';temporary.mkdir(parents=True)
        (temporary/'SingletonLock').symlink_to('old-browser-pid')
        (self.root/'.leases.json').write_text(json.dumps({'dead':dict(pid=os.getpid(),identity='old',reserved=1024,verified=False,browserTemporary=temporary.name)}))
        with ws.job('ph','next',1024):pass
        self.assertFalse(temporary.exists());self.assertTrue((folder/'source.mp4').exists())

if __name__=='__main__':unittest.main()
