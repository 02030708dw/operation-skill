import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hm_media_compat as media


@unittest.skipUnless(shutil.which(os.environ.get('FFMPEG', 'ffmpeg')) and shutil.which(os.environ.get('FFPROBE', 'ffprobe')), 'FFmpeg tools required')
class MediaCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def fixture(self, codec='libvpx-vp9', audio=True, size='120x200'):
        path = self.root / 'source.mp4'
        cmd = [os.environ.get('FFMPEG','ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size='+size+':rate=30']
        if audio: cmd += ['-f','lavfi','-i','sine=frequency=440:sample_rate=48000']
        cmd += ['-t','0.8','-c:v',codec,'-threads','2','-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(path)]
        subprocess.run(cmd,check=True,capture_output=True)
        return path
    def test_compatible_skip(self):
        path=self.fixture('libx264'); before=media.sha256(path)
        result=media.normalize(path)
        self.assertFalse(result['converted']); self.assertEqual(result['path'],str(path.resolve())); self.assertEqual(before,result['sha256'])
    def test_vp9_portrait_audio_and_idempotence(self):
        path=self.fixture(); original=media.sha256(path); result=media.normalize(path)
        self.assertTrue(result['converted']); self.assertEqual(media.sha256(path),original)
        info=media.probe(result['path']); self.assertTrue(media.compatible(result['path'],info))
        v=info['streams'][0]; self.assertEqual((v['width'],v['height']),(120,200))
        self.assertTrue(any(s.get('profile')=='LC' for s in info['streams']))
        with mock.patch.object(media,'run',side_effect=AssertionError('must reuse verified result')):
            self.assertEqual(result,media.normalize(path))
    def test_he_aac_is_transcoded_to_lc_with_audio(self):
        # Generated locally from a 440 Hz sine wave using AudioToolbox HE-AAC;
        # retaining the tiny fixture makes this test portable to Linux FFmpeg.
        import base64
        audio=self.root/'heaac.m4a'
        audio.write_bytes(base64.b64decode((Path(__file__).parent/'fixtures/heaac-sine.m4a.b64').read_bytes()))
        source=self.root/'he-aac.mp4'
        subprocess.run([os.environ.get('FFMPEG','ffmpeg'),'-v','error','-y',
            '-i',str(self.fixture('libx264',audio=False)),'-i',str(audio),
            '-map','0:v:0','-map','1:a:0','-c','copy','-shortest','-movflags','+faststart',str(source)],check=True,capture_output=True)
        before=media.probe(source)
        self.assertEqual(next(s['profile'] for s in before['streams'] if s['codec_type']=='audio'),'HE-AAC')
        self.assertFalse(media.compatible(source,before))
        result=media.normalize(source)
        self.assertTrue(result['converted'])
        after=media.probe(result['path'])
        self.assertEqual(next(s['profile'] for s in after['streams'] if s['codec_type']=='audio'),'LC')
        self.assertTrue(media.compatible(result['path'],after))
        media.validate(before,after)

    def test_rotation_and_frame_rate(self):
        source=self.fixture(size='200x120')
        rotated=self.root/'rotated.mp4'
        data=bytearray(source.read_bytes()); matrix=data.index(b'tkhd')+44
        data[matrix:matrix+36]=struct.pack('>9i',0,65536,0,-65536,0,0,0,0,1<<30)
        rotated.write_bytes(data)
        result=media.normalize(rotated); video=media.probe(result['path'])['streams'][0]
        self.assertEqual((video['width'],video['height']),(120,200))
        self.assertFalse(any(x.get('rotation') for x in video.get('side_data_list',[])))

    def test_no_audio(self):
        result=media.normalize(self.fixture(audio=False))
        self.assertEqual([s['codec_type'] for s in media.probe(result['path'])['streams']],['video'])
    def test_corrupt_preserves_source_and_reports_failed(self):
        path=self.root/'broken.mp4'; path.write_bytes(b'not media')
        with mock.patch.dict(os.environ,{'HM_MEDIA_COMPAT_ENABLED':'1'}):
            video={'localPath':str(path),'statistics':{'outcome':'SUCCESS'}}; media.prepare_record(video)
        self.assertEqual(video['status'],'download-failed'); self.assertEqual(video['statistics']['outcome'],'FAILURE')
        self.assertEqual(path.read_bytes(),b'not media'); self.assertFalse(list(self.root.rglob('*.json')))
    def test_interruption_cleans_temporary_and_retries(self):
        path=self.fixture(); original=media.sha256(path); real=media.run
        def interrupt(argv):
            if 'libx264' in argv: raise KeyboardInterrupt()
            return real(argv)
        with mock.patch.object(media,'run',side_effect=interrupt), self.assertRaises(KeyboardInterrupt): media.normalize(path)
        self.assertEqual(media.sha256(path),original); self.assertFalse(list(self.root.rglob('*.json')))
        self.assertFalse(list((self.root/'.hm-compatible').glob('*.mp4')))
        self.assertTrue(media.normalize(path)['converted'])
    def test_tampered_cache_is_rebuilt(self):
        path=self.fixture(); result=media.normalize(path); Path(result['path']).write_bytes(b'bad')
        self.assertEqual(media.normalize(path)['sha256'],result['sha256'])
    def test_changed_duration_and_sync_are_rejected(self):
        info=media.probe(self.fixture()); changed=json.loads(json.dumps(info)); changed['format']['duration']='200'
        with self.assertRaises(media.CompatibilityError): media.validate(info,changed)
        changed=json.loads(json.dumps(info)); changed['streams'][1]['start_time']='2'
        with self.assertRaises(media.CompatibilityError): media.validate(info,changed)


class IngressGateTest(unittest.TestCase):
    def test_ingress_keeps_original_download_name_and_title(self):
        video=dict(localPath='/downloads/original.webm',fileName='原标题.第1集.webm',title='原标题🎬')
        result=dict(path='/downloads/.hm-compatible/v1-hash.mp4',fileSize=42,sha256='a'*64,durationSeconds=1.0)
        with mock.patch.dict(os.environ,{'HM_MEDIA_COMPAT_ENABLED':'1'}),mock.patch.object(media,'normalize',return_value=result):
            media.prepare_record(video)
        self.assertEqual(video['fileName'],'原标题.第1集.mp4')
        self.assertEqual(video['title'],'原标题🎬')
        self.assertEqual(video['localPath'],result['path'])

    def test_failed_conversion_is_never_recorded_as_downloaded(self):
        import facebook_video_ingest as ingest
        video=dict(localPath='/missing/source.mp4',originalUrl='https://example.test/1',status='downloaded',statistics={'outcome':'SUCCESS'})
        with mock.patch.dict(os.environ,{'HM_MEDIA_COMPAT_ENABLED':'1'}), mock.patch.object(ingest,'api_call') as api:
            ingest.record_video('backend','token','worker','execution',video,download_status='DOWNLOADED',upload_status='PENDING')
        payload=api.call_args.args[4]
        self.assertEqual(payload['downloadStatus'],'DOWNLOAD_FAILED')
        self.assertEqual(payload['errorCode'],'MEDIA_COMPAT_FAILED')
        self.assertEqual(video['status'],'download-failed')

    def test_final_manifest_inherits_stream_compatibility_result(self):
        import facebook_video_ingest as ingest
        recorder=ingest.IncrementalVideoRecorder('backend','token','worker','execution')
        video=dict(originalUrl='https://example.test/1',status='downloaded')
        identity=ingest.video_result_identity(video)
        recorder.recorded.add(identity)
        recorder.normalized_results[identity]=dict(video,status='download-failed',errorCode='MEDIA_COMPAT_FAILED')
        recorder.finish([video])
        self.assertEqual(video['status'],'download-failed')


if __name__=='__main__': unittest.main()
