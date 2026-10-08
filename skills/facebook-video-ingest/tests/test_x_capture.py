import sys
from pathlib import Path
import unittest
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hm_x_capture as x
import hm_public_capture as public
import hm_pipeline_discovery as discovery


class XCaptureTests(unittest.TestCase):
    def test_local_handoff_resolves_native_ids_without_a_browser_session(self):
        job={'sourceUrl':'https://x.com/creator/media','localDiscovery':{'readFromTop':True,'urls':[f'https://x.com/creator/status/{i}' for i in range(200,190,-1)]}}
        def resolve(url, limit):
            return [{'id':str(int(url.rsplit('/',1)[1])+1000),'url':url}]
        with patch.object(x,'discover',side_effect=resolve) as discover:
            result=x.local_profile_check(job)
        self.assertEqual('SUCCESS',result['outcome']);self.assertEqual('1200',result['entries'][0]['id'])
        self.assertEqual(10,discover.call_count)
        self.assertTrue(all(len(call.args)==2 for call in discover.call_args_list))

    def test_local_partial_handoff_keeps_native_ids_but_never_marks_complete(self):
        job={'sourceUrl':'https://x.com/creator/media','localDiscovery':{'readFromTop':True,'urls':['https://x.com/creator/status/200']}}
        with patch.object(x,'discover',return_value=[{'id':'999','url':job['localDiscovery']['urls'][0]}]):
            with self.assertRaises(public.Failure) as caught:x.local_profile_check(job)
        self.assertEqual('DISCOVERY_INCOMPLETE',caught.exception.code)
        self.assertEqual('999',caught.exception.entries[0]['id'])
        for urls in (['https://x.com/other/status/200'],['https://x.com/creator/status/100','https://x.com/creator/status/200']):
            job['localDiscovery']['urls']=urls
            with patch.object(x,'discover',return_value=[]),self.assertRaises(public.Failure):x.local_profile_check(job)

    def test_normalizes_profiles_posts_and_rejects_untrusted_routes(self):
        self.assertEqual({'id': '123', 'url': 'https://x.com/Creator/status/123'},
            x.source('https://mobile.twitter.com/Creator/status/123/?s=20#video'))
        self.assertEqual({'kind':'profile','handle':'creator','url':'https://x.com/creator/media'},x.source('https://twitter.com/Creator/?s=20'))
        self.assertIsNone(public.single_source('X','https://x.com/Creator/media'))
        for url in ('https://x.com/home', 'https://x.com/Creator/likes',
                    'http://x.com/Creator/status/123', 'https://x.com.evil/Creator/status/123',
                    'https://secret@x.com/Creator/status/123', 'https://x.com:443/Creator/status/123',
                    'https://x.com/Creator/%73tatus/123'):
            with self.assertRaises(public.Failure, msg=url): x.source(url)

    def test_partial_profile_result_retains_video_identities_but_is_never_complete(self):
        entries=[{'id':'456','url':'https://x.com/creator/status/123'}]
        proc=SimpleNamespace(stdout='HM_X_DISCOVERY '+json.dumps(dict(entries=entries,validated=True,sourceExhausted=False)))
        with patch.object(x.subprocess,'run',return_value=proc) as run:
            with self.assertRaises(public.Failure) as caught:x.discover_profile('https://x.com/creator/media',10,'/private/profile')
        self.assertEqual('DISCOVERY_INCOMPLETE',caught.exception.code);self.assertEqual(entries,caught.exception.entries)
        self.assertIn('/private/profile',run.call_args.args[0]);self.assertEqual(140,run.call_args.kwargs['timeout'])
        with self.assertRaises(public.Failure) as caught:x.discover_profile('https://x.com/creator',10,None)
        self.assertEqual('LOGIN_REQUIRED',caught.exception.code)

    def test_profile_uses_only_its_regional_x_session_and_releases_the_lease(self):
        import hm_x_account as accounts
        config={'xAccount':{'key':'x-th'},'facebookAccount':{'key':'fb-th'},'googleAccount':{'key':'google-th'}}
        lease=Mock(profile=Path('/private/x-th/profile'))
        with patch.object(accounts,'read_state',return_value={'state':'LOGGED_IN'}),patch.object(accounts,'acquire_capture',return_value=lease),patch.object(x,'discover_profile',return_value=[{'id':'456','url':'https://x.com/creator/status/123'}]) as discover,patch.object(public.facebook,'account_config') as fb,patch.object(public.google,'account_config') as google:
            result=x.profile_check({'sourceUrl':'https://x.com/creator/media'},config)
            self.assertTrue(result['complete']);discover.assert_called_once_with('https://x.com/creator/media',10,lease.profile)
            lease.close.assert_called_once();fb.assert_not_called();google.assert_not_called()
        with self.assertRaises(ValueError):accounts.account_config({'xAccount':{'key':'x-other'}})
        with tempfile.TemporaryDirectory() as temp,patch.dict('os.environ',{'HM_X_ACCOUNT_ROOT':temp}):
            admin=accounts.acquire(config['xAccount']);self.assertIsNone(accounts.acquire_capture(config['xAccount']));admin.close()
            reader=accounts.acquire_capture(config['xAccount']);self.assertIsNone(accounts.acquire(config['xAccount']));reader.close()

    def test_check_uses_media_ids_and_finds_all_videos_in_one_post(self):
        rows=[{'id': str(i), 'formats': [{'url': 'https://video.twimg.com/video.mp4'}]} for i in (456, 789)]
        ydl=Mock();ydl.__enter__=Mock(return_value=ydl);ydl.__exit__=Mock(return_value=False)
        ydl.extract_info.return_value={'_type': 'playlist', 'id': '123', 'entries': rows}
        with patch('yt_dlp.YoutubeDL', return_value=ydl) as factory:
            result=discovery.check({'platform': 'X', 'sourceUrl': 'https://x.com/Creator/status/123', 'latestTen': True}, {'cookiefile': 'must-not-read'})
        self.assertEqual(['456', '789'], [item['id'] for item in result['entries']])
        self.assertTrue(result['validated']);self.assertTrue(result['complete'])
        self.assertNotIn('cookiefile', factory.call_args.args[0])
        self.assertEqual(rows[1], x.select_info({'_type': 'playlist', 'entries': rows}, '789'))
        with self.assertRaises(public.Failure): x.select_info(rows[0], '789')

    def test_does_not_treat_external_links_empty_posts_or_live_streams_as_native_video(self):
        for info in ({'id': 'youtube-id', 'formats': [{}]}, {'id': '123'},
                     {'_type': 'playlist', 'entries': []},
                     {'id': '123', 'formats': [{}], 'is_live': True}):
            with self.assertRaises(public.Failure): x.videos(info)

    def test_login_errors_never_use_facebook_or_google_accounts(self):
        accounts={'facebookAccount': {'key': 'fb'}, 'googleAccount': {'key': 'google'}}
        with patch.object(public.facebook, 'account_config') as fb, patch.object(public.google, 'account_config') as google:
            with self.assertRaises(public.Failure):
                public.attempt(Mock(side_effect=public.Failure('LOGIN_REQUIRED')), accounts, 'X', [], 'DOWNLOAD')
            self.assertFalse(public.saved_session_available(accounts, 'X'))
            public.account_outcome(accounts, 'X', 'LOGIN_REQUIRED')
            fb.assert_not_called();google.assert_not_called()


if __name__ == '__main__': unittest.main()
