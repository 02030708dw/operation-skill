import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import hm_capture_pipeline_worker as pipeline
import hm_pipeline_discovery as discovery
import hm_public_capture as public
import hm_server_worker as runner


def job(**changes):
    return dict(jobNo='P-public',leaseVersion=1,tenant='ph',stage='CHECK',platform='Facebook',
                sourceUrl='https://www.facebook.com/fixture',latestTen=True,windowedMode=True,**changes)


class PublicDiscoveryTests(unittest.TestCase):
    def test_windowed_x_profile_reads_its_x_session_and_queues_media_identities(self):
        import hm_x_capture as x
        entry={'id':'456','url':'https://x.com/creator/status/123'}
        config={'xAccount':{'key':'x-ph'}}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'job.json';value=job();value.update(platform='X',sourceUrl='https://x.com/creator/media');pipeline.atomic_json(path,value)
            with patch.object(runner,'tenant_config',return_value=config),patch.object(x,'profile_check',return_value=dict(outcome='SUCCESS',complete=True,validated=True,entries=[entry],scope='LATEST_TEN',evidence='VALIDATED_LATEST_TEN')) as profile,patch.object(public,'authenticated_session') as unrelated:
                pipeline.execute_stage(path)
            result=json.loads(pipeline.result_file(path).read_text())
            profile.assert_called_once();unrelated.assert_not_called()
            self.assertEqual([entry],result['entries']);self.assertEqual('AUTHENTICATED',result['attempts'][0]['mode']);self.assertTrue(result['complete'])

    def test_login_gated_feed_keeps_valid_deduplicated_links_without_authenticated_discovery(self):
        entries=[{'id':'123','url':'https://www.facebook.com/reel/123'},
                 {'id':'123','url':'https://www.facebook.com/watch/?v=123'},
                 {'id':'456','url':'https://evil.example/reel/456'},
                 {'id':'789','url':'https://www.facebook.com/watch/?v=789'}]
        payload={'errorCode':'LOGIN_REQUIRED','entries':entries}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'job.json';pipeline.atomic_json(path,job())
            with patch.object(runner,'tenant_config',return_value={}),\
                 patch.object(discovery.subprocess,'run',return_value=SimpleNamespace(stdout='HM_PUBLIC_DISCOVERY '+json.dumps(payload))),\
                 patch.object(public,'authenticated_session') as auth:
                pipeline.execute_stage(path)
            result=json.loads(pipeline.result_file(path).read_text())
            auth.assert_not_called()
            self.assertEqual('INCOMPLETE',result['outcome']);self.assertFalse(result['complete'])
            self.assertFalse(result['validated']);self.assertEqual('LOGIN_REQUIRED',result['errorCode'])
            self.assertEqual(['123','789'],[entry['id'] for entry in result['entries']])
            self.assertEqual([{'stage':'DISCOVERY','mode':'PUBLIC','result':'PARTIAL','reasonCode':'LOGIN_REQUIRED'}],result['attempts'])

    def test_direct_video_urls_go_to_download_without_metadata_preflight(self):
        cases=[('Facebook','https://www.facebook.com/reel/123','123'),
               ('YouTube','https://www.youtube.com/watch?v=abcdefghijk','abcdefghijk'),
               ('TikTok','https://www.tiktok.com/@fixture/video/123','123')]
        for platform,url,identity in cases:
            with self.subTest(platform=platform),patch.object(discovery,'check',side_effect=AssertionError('must not inspect metadata')):
                result=discovery.public_check(dict(job(),platform=platform,sourceUrl=url))
            self.assertEqual(identity,result['entries'][0]['id'])
            self.assertEqual('DIRECT_VIDEO_LINK',result['evidence'])
            self.assertFalse(result['complete']);self.assertEqual('INCOMPLETE',result['outcome'])

    def test_short_or_unverified_feed_retains_links_without_successful_coverage(self):
        entries=[{'id':'abcdefghijk','url':'https://www.youtube.com/watch?v=abcdefghijk'}]
        for validated,complete in ((True,False),(False,False),(False,True)):
            page=dict(entries=entries,validated=validated,complete=complete,outcome='INCOMPLETE')
            with self.subTest(validated=validated,complete=complete),patch.object(discovery,'youtube',return_value=page):
                result=discovery.public_check(dict(job(),platform='YouTube',sourceUrl='https://www.youtube.com/@fixture'))
            self.assertEqual(entries,result['entries']);self.assertEqual('INCOMPLETE',result['outcome'])
            self.assertFalse(result['complete']);self.assertFalse(result['validated'])

    def test_validated_full_feed_and_validated_empty_feed_keep_success_evidence(self):
        for count in (0,10):
            entries=[{'id':str(i),'url':'https://www.facebook.com/watch/?v='+str(i)} for i in range(count)]
            with patch.object(discovery,'facebook',return_value=dict(entries=entries,validated=True,complete=True)):
                result=discovery.public_check(job())
            self.assertEqual('SUCCESS',result['outcome']);self.assertTrue(result['complete'])
            self.assertEqual('VALIDATED_LATEST_TEN',result['evidence']);self.assertEqual(count,len(result['entries']))

    def test_malformed_id_or_url_never_enters_partial_download_queue(self):
        entries=[None,{}, {'id':'1','url':'http://www.facebook.com/reel/1'},
                 {'id':'1','url':'https://www.facebook.com:443/reel/1'},
                 {'id':'1','url':'https://user@www.facebook.com/reel/1'},
                 {'id':'1','url':'https://www.facebook.com/reel/2'},
                 {'id':'1','url':'https://www.facebook.com/creator'},
                 {'id':'abc','url':'https://www.facebook.com/reel/abc'},
                 {'id':'1','url':'https://www.facebook.com.evil.example/reel/1'}]
        self.assertEqual([],discovery.valid_public_entries('Facebook',entries))
        self.assertEqual([],discovery.valid_public_entries('YouTube',[{'id':'abcdefghijk','url':'https://evil.example/watch?v=abcdefghijk'}]))
        entries=[{'id':str(i),'url':'https://www.facebook.com/reel/'+str(i)} for i in range(20)]
        self.assertEqual(10,len(discovery.valid_public_entries('Facebook',entries)))

    def test_inconclusive_facebook_provider_keeps_links_even_without_cursor(self):
        entry={'id':'123','url':'https://www.facebook.com/reel/123'}
        with patch.object(discovery.subprocess,'run',return_value=SimpleNamespace(stdout='HM_PUBLIC_DISCOVERY '+json.dumps({'entries':[entry]}))):
            result=discovery.public_check(job())
        self.assertEqual(['123'],[entry['id'] for entry in result['entries']])
        self.assertFalse(result['complete'])

    def test_partial_youtube_iterator_failure_keeps_already_discovered_links(self):
        def rows():
            yield {'id':'abcdefghijk'}
            raise public.Failure('VERIFICATION_REQUIRED')
        class Ydl:
            def __init__(self,*args,**kwargs):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def extract_info(self,*args,**kwargs):return {'id':'fixture','entries':rows()}
        with patch('yt_dlp.YoutubeDL',Ydl):
            result=discovery.public_check(dict(job(),platform='YouTube',sourceUrl='https://www.youtube.com/@fixture'))
        self.assertEqual('INCOMPLETE',result['outcome']);self.assertEqual('VERIFICATION_REQUIRED',result['errorCode'])
        self.assertEqual('abcdefghijk',result['entries'][0]['id'])

    def test_empty_login_failure_does_not_try_account_or_create_platform_cooldown(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'job.json';pipeline.atomic_json(path,job())
            with patch.dict(os.environ,{'HM_SERVER_STATE_DIR':temp}),\
                 patch.object(runner,'tenant_config',return_value={}),\
                 patch.object(discovery,'public_check',side_effect=public.Failure('VERIFICATION_REQUIRED')),\
                 patch.object(public,'authenticated_session') as auth:
                pipeline.execute_stage(path)
            result=json.loads(pipeline.result_file(path).read_text())
            self.assertEqual('VERIFICATION_REQUIRED',result['errorCode']);self.assertNotEqual('SUCCESS',result['outcome'])
            auth.assert_not_called();self.assertFalse((root/'public-cooldown/Facebook.json').exists())

    def test_only_real_rate_limits_cool_down_new_pipeline_including_partial_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);cooldown=root/'public-cooldown/Facebook.json'
            for code,active in ((None,True),('RATE_LIMITED',True),('VERIFICATION_REQUIRED',False),('ACCOUNT_SUSPENDED',False)):
                pipeline.atomic_json(cooldown,dict(until=time.time()+3600,reasonCode=code))
                self.assertEqual(active,pipeline.platform_cooling_down(cooldown,True))
                self.assertTrue(pipeline.platform_cooling_down(cooldown,False))
            path=root/'job.json';pipeline.atomic_json(path,job())
            partial=discovery.partial_result([{'id':'1','url':'https://www.facebook.com/reel/1'}],'RATE_LIMITED')
            with patch.dict(os.environ,{'HM_SERVER_STATE_DIR':temp}),patch.object(runner,'tenant_config',return_value={}),patch.object(discovery,'public_check',return_value=partial):
                pipeline.execute_stage(path)
            self.assertTrue(pipeline.platform_cooling_down(cooldown,True))
            self.assertEqual('RATE_LIMITED',json.loads(cooldown.read_text())['reasonCode'])

    def test_one_video_login_failure_does_not_prevent_next_anonymous_download(self):
        for failure in ('LOGIN_REQUIRED','VERIFICATION_REQUIRED'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);calls=[]
                def download(platform,entry,directory,credentials):
                    calls.append((entry['id'],credentials))
                    if entry['id']=='1':raise public.Failure(failure)
                    path=directory/'video.mp4';path.write_bytes(b'public-video')
                    return dict(path=str(path),title='public',duration=1)
                def store(video,*args):
                    return dict(video,localPath=None,reviewObjectKey='review/PH/test/source.mp4',reviewBucket='fixture',reviewKeyVersion='v1')
                with patch.dict(os.environ,{'HM_SERVER_STATE_DIR':temp}),\
                     patch.object(runner,'tenant_config',return_value={'mediaRoot':temp}),\
                     patch.object(public,'download_item',side_effect=download),\
                     patch.object(public,'authenticated_session',side_effect=public.Failure('ACCOUNT_UNAVAILABLE')),\
                     patch.object(pipeline,'store_review_original',side_effect=store):
                    results=[]
                    for identity in ('1','2'):
                        path=root/(identity+'.json')
                        pipeline.atomic_json(path,dict(job(),stage='DOWNLOAD',subjectKey=identity,entry={'id':identity,'url':'https://www.facebook.com/reel/'+identity}))
                        pipeline.execute_stage(path);results.append(json.loads(pipeline.result_file(path).read_text()))
                self.assertNotEqual('SUCCESS',results[0]['outcome']);self.assertEqual('SUCCESS',results[1]['outcome'])
                self.assertEqual([('1',{}),('2',{})],calls)
                self.assertFalse((root/'public-cooldown/Facebook.json').exists())


if __name__=='__main__':unittest.main()
