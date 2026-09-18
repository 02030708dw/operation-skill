import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import browser_discovery as b
import download as d
SOURCE = d.normalize_url('https://www.tiktok.com/@example')

def row(vid): return {'id':str(vid), 'url':SOURCE['url']+'/video/'+str(vid)}

class FakeBrowser:
    frames = []
    status = 200
    limited = False
    last = None
    def __init__(self, root, deadline):
        self.document_status = self.status; self.rate_limited = self.limited
        self.index = self.scrolls = 0; self.closed = False
        type(self).last = self
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def call(self, *args): return {}
    def snapshot(self, account):
        value = self.frames[min(self.index,len(self.frames)-1)]; self.index += 1
        return {'host':'www.tiktok.com','entries':[], **value}
    def scroll(self): self.scrolls += 1

class BrowserTests(unittest.TestCase):
    def setUp(self):
        FakeBrowser.status = 200; FakeBrowser.limited = False
    def discover(self, frames, limit=10):
        FakeBrowser.frames = frames
        return b.discover(SOURCE, limit, None, FakeBrowser, pause=0)
    def test_reaches_limit_without_following_foreign_links(self):
        foreign = {'url':'https://www.tiktok.com/@other/video/999'}
        result = self.discover([{'entries':[foreign,row(1),row(1)]},{'entries':[row(1),row(2),row(3)]}],2)
        self.assertEqual([x['id'] for x in result['entries']],['1','2'])
        self.assertTrue(result['complete']); self.assertTrue(FakeBrowser.last.closed)
    def test_three_unchanged_scans_mark_partial(self):
        result = self.discover([{'entries':[row(1)]}])
        self.assertFalse(result['complete']); self.assertEqual(FakeBrowser.last.scrolls,3)
    def test_ten_scrolls_is_hard_limit(self):
        frames = [{'entries':[row(i)]} for i in range(1,20)]
        result = self.discover(frames,100)
        self.assertEqual(FakeBrowser.last.scrolls,10)
        self.assertEqual(len(result['entries']),11)
        self.assertFalse(result['complete'])
    def test_stop_for_verification_even_if_links_visible(self):
        with self.assertRaises(b.BrowserFailure) as raised:
            self.discover([{'entries':[row(1)],'challenge':True}])
        self.assertEqual(raised.exception.code,'VERIFICATION_REQUIRED')
        self.assertTrue(FakeBrowser.last.closed)
    def test_explicit_login_access_and_rate(self):
        for field, code in [('login','LOGIN_REQUIRED'),('unavailable','ACCESS_DENIED')]:
            with self.assertRaises(b.BrowserFailure) as raised: self.discover([{field:True}])
            self.assertEqual(raised.exception.code,code)
        FakeBrowser.limited = True
        with self.assertRaises(b.BrowserFailure) as raised: self.discover([{}])
        self.assertEqual(raised.exception.code,'RATE_LIMITED')
    def test_empty_is_not_login(self):
        with self.assertRaises(b.BrowserFailure) as raised: self.discover([{}])
        self.assertEqual(raised.exception.code,'DISCOVERY_EMPTY')
        self.assertEqual(FakeBrowser.last.scrolls,2)
    def test_only_parser_errors_trigger_browser_fallback(self):
        for code in ('LOGIN_REQUIRED','RATE_LIMITED','VERIFICATION_REQUIRED','NETWORK_ERROR','ACCESS_DENIED'):
            with patch.object(d,'network_call',side_effect=d.Failure(code)), patch.object(b,'discover') as fallback:
                with self.assertRaises(d.Failure): d.Extractor('ffmpeg').discover(SOURCE,10)
                fallback.assert_not_called()
        with patch.object(d,'network_call',side_effect=d.Failure('PROFILE_ID_UNAVAILABLE')):
            with patch.object(b,'discover',return_value={'entries':[row(1)],'complete':False,'evidence':{'httpStatus':200}}):
                extractor = d.Extractor('ffmpeg')
                self.assertEqual(extractor.discover(SOURCE,10),[row(1)])
                self.assertTrue(extractor.discovery['incomplete'])
    def test_failed_browser_keeps_sanitized_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            with patch.object(d,'network_call',side_effect=d.Failure('PROFILE_ID_UNAVAILABLE')):
                with patch.object(b,'discover',side_effect=b.BrowserFailure('VERIFICATION_REQUIRED',{'httpStatus':200,'challengeVisible':True})):
                    result = d.run(SOURCE,output,10,True,d.Extractor('ffmpeg'),'ffmpeg',output/'report.json')
            self.assertEqual(result['status'],'STOPPED')
            self.assertIsNone(result['discovered'])
            self.assertTrue(result['discovery']['evidence']['challengeVisible'])
            self.assertEqual(result['counts']['failed'],0)
    def test_partial_enumeration_does_not_claim_zero_unattempted(self):
        extractor = d.Extractor('ffmpeg')
        extractor.discovery = {'incomplete':True,'method':'ANONYMOUS_CHROMIUM'}
        with tempfile.TemporaryDirectory() as temp, patch.object(extractor,'discover',return_value=[row(1)]):
            output = Path(temp)
            result = d.run(SOURCE,output,10,True,extractor,'ffmpeg',output/'report.json')
            self.assertEqual(result['status'],'PARTIAL')
            self.assertIsNone(result['counts']['unattempted'])
    def test_startup_failure_cleans_temporary_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(b,'chromium_binary',return_value='/missing/chromium'):
                with self.assertRaises(b.BrowserFailure):
                    with b.Chromium(temp, b.time.monotonic()+5): pass
            self.assertEqual(list(Path(temp).iterdir()),[])

if __name__ == '__main__': unittest.main()
