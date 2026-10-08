import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hm_x_capture as x
import hm_public_capture as public
import hm_pipeline_discovery as discovery


class XCaptureTests(unittest.TestCase):
    def test_normalizes_posts_but_rejects_profiles_and_untrusted_urls(self):
        self.assertEqual({'id': '123', 'url': 'https://x.com/Creator/status/123'},
            x.source('https://mobile.twitter.com/Creator/status/123/?s=20#video'))
        for url in ('https://x.com/Creator/media', 'https://x.com/Creator',
                    'http://x.com/Creator/status/123', 'https://x.com.evil/Creator/status/123',
                    'https://secret@x.com/Creator/status/123', 'https://x.com:443/Creator/status/123',
                    'https://x.com/Creator/%73tatus/123'):
            with self.assertRaises(public.Failure, msg=url): x.source(url)

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
