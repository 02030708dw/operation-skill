import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hm_public_capture as public
import hm_tiktok_capture as tiktok
import hm_server_worker as worker

URL='https://www.tiktok.com/@anunya.pangya/video/7686495975761939733'
PROFILE='https://www.tiktok.com/@anunya.pangya'


class TikTokCaptureTests(unittest.TestCase):
    def test_anonymous_login_never_borrows_facebook_or_google(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tiktok,'download',side_effect=tiktok.downloader.Failure('LOGIN_REQUIRED')), \
             patch.object(public.facebook,'account_config') as fb, patch.object(public.google,'account_config') as google:
            root=Path(tmp)
            report=public.run_download('TikTok',URL,10,root/'out',root/'run.json',{'facebookAccount':{'key':'fb'}})
            self.assertEqual('LOGIN_REQUIRED',report['status'])
            self.assertEqual(1,report['counts']['login-required'])
            self.assertEqual(0,report['counts']['failed'])
            fb.assert_not_called();google.assert_not_called()

    def test_discovery_errors_are_not_login_and_counts_remain_unknown(self):
        for code in ('DISCOVERY_EMPTY','PROFILE_ID_UNAVAILABLE','EXTRACTION_ERROR','ACCESS_DENIED'):
            with tempfile.TemporaryDirectory() as tmp, patch.object(tiktok,'discover',side_effect=tiktok.downloader.Failure(code)):
                root=Path(tmp);report=public.run_download('TikTok',PROFILE,10,root/'out',root/'run.json',{})
                self.assertEqual(code,report['errorCode']);self.assertEqual('FAILED',report['status'])
                self.assertIsNone(report['counts']['unattempted'])
                self.assertEqual('NONE',report['loginRestriction']['scope'])

    def test_receipt_survives_callback_failure_and_missing_file_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(public.time,'sleep'):
            root=Path(tmp);out=root/'out'
            def download(item,output):
                path=output/'preview.mp4';path.write_bytes(b'valid-fixture')
                digest=hashlib.sha256(path.read_bytes()).hexdigest()
                return dict(item,status='downloaded',path=str(path),sha256=digest,originalPath=str(path),originalSha256=digest)
            with patch.object(tiktok,'download',side_effect=download) as calls:
                with self.assertRaisesRegex(RuntimeError,'callback'):
                    public.run_download('TikTok',URL,10,out,root/'run.json',{},Mock(side_effect=RuntimeError('callback')))
                report=public.run_download('TikTok',URL,10,out,root/'run.json',{})
                self.assertEqual(1,report['counts']['downloaded']);self.assertEqual(1,calls.call_count)
                again=public.run_download('TikTok',URL,10,out,root/'again.json',{})
                self.assertEqual(1,again['counts']['skipped']);self.assertEqual(1,calls.call_count)
                record=public.video_record(again['results'][0]);self.assertEqual('CACHE',record['statistics']['outcome'])
                (out/'preview.mp4').unlink()
                recovered=public.run_download('TikTok',URL,10,out,root/'recover.json',{})
                self.assertEqual(1,recovered['counts']['downloaded']);self.assertEqual(2,calls.call_count)

    def test_partial_discovery_never_claims_complete_inventory(self):
        items=tiktok.Entries([{'id':'123','url':PROFILE+'/video/123'}]);items.discovery={'method':'ANONYMOUS_CHROMIUM','incomplete':True}
        with tempfile.TemporaryDirectory() as tmp,patch.object(tiktok,'discover',return_value=items),patch.object(tiktok,'download',return_value={'status':'filtered-duration'}):
            root=Path(tmp);report=public.run_download('TikTok',PROFILE,10,root/'out',root/'run.json',{})
            self.assertEqual('PARTIAL',report['status']);self.assertIsNone(report['counts']['unattempted'])

    def test_photos_live_and_duration_do_not_count_as_downloads(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(tiktok,'provider',return_value=Mock()):
            for url in (PROFILE+'/photo/123',PROFILE+'/live'):
                root=Path(tmp);report=public.run_download('TikTok',url,10,root/'out',root/(hashlib.sha256(url.encode()).hexdigest()+'.json'),{})
                self.assertEqual(1,report['counts']['unsupported']);self.assertEqual(0,report['counts']['downloaded'])
        with tempfile.TemporaryDirectory() as tmp,patch.object(tiktok,'provider',return_value=Mock(inspect=Mock(return_value={'duration':1201,'formats':[{}]}))):
            self.assertEqual('filtered-duration',tiktok.download({'id':'123','url':PROFILE+'/video/123'},Path(tmp))['status'])

    def test_preview_is_h264_aac_and_original_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);raw=root/'originals';raw.mkdir()
            original=raw/'20260918_test_[123].mp4';ffmpeg=tiktok.downloader.ffmpeg_binary()
            subprocess.run([ffmpeg,'-nostdin','-v','error','-f','lavfi','-i','color=size=64x64:rate=10',
                '-f','lavfi','-i','sine=frequency=1000','-t','0.5','-c:v','mpeg4','-c:a','aac',str(original)],check=True)
            verified=tiktok.downloader.validate_file(original,ffmpeg)
            saved=dict(verified,path=original.name,title='fixture')
            engine=Mock(ffmpeg=ffmpeg,inspect=Mock(return_value={'formats':[{}],'duration':0.5}))
            with patch.object(tiktok,'provider',return_value=engine),patch.object(tiktok.downloader,'download_one',return_value=saved):
                item=tiktok.download({'id':'123','url':PROFILE+'/video/123'},root)
            preview=tiktok.downloader.validate_file(Path(item['path']),ffmpeg)
            self.assertEqual('h264',preview['videoCodec']);self.assertEqual('aac',preview['audioCodec'])
            self.assertEqual(verified['sha256'],tiktok.downloader.digest(original));self.assertTrue(tiktok.receipt_valid(item))

    def test_platform_opt_in_and_locks_are_regional(self):
        spec=dict(dispatchId=1,attempt=1,kind='CAPTURE',slot=1,taskNo='C-X',executionNo='E-X',tenant='vn',platform='TikTok',capturePolicy='PUBLIC_FIRST')
        with patch.object(worker,'tenant_config',return_value={'capturePolicy':'PUBLIC_FIRST','enabledPlatforms':['Facebook']}):
            with self.assertRaises(ValueError):worker.validate_spec(spec)
        config={'capturePolicy':'PUBLIC_FIRST','enabledPlatforms':['Facebook','YouTube','TikTok']}
        with patch.object(worker,'tenant_config',return_value=config):
            paths=set()
            for region in ('vn','ph','th','id'):
                spec['tenant']=region;self.assertEqual('TikTok',worker.validate_spec(spec)['platform'])
                paths.add(str(worker.tenant_root(spec)/'locks/platform-TikTok.lock'))
            self.assertEqual(4,len(paths))

if __name__=='__main__':unittest.main()
