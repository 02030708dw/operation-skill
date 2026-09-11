import importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('stats_pipeline',Path(__file__).parents[1]/'scripts/facebook_video_ingest.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
class StatisticsTests(unittest.TestCase):
 def video(self,outcome='SUCCESS',status='downloaded'):
  return {'originalUrl':'https://www.facebook.com/reel/123','platformVideoId':'123','status':status,'statistics':{'attemptId':'stable_attempt_1','occurredAt':'2026-09-10T23:59:59+08:00','outcome':outcome}}
 @patch.object(p,'api_call')
 def test_replay_preserves_identity_and_date(self,call):
  with tempfile.TemporaryDirectory() as directory:
   journal=Path(directory)/'statistics.jsonl';journal.write_text(json.dumps(self.video())+'\n{"incomplete":')
   p.replay_statistics('backend','token','worker','E-TEST',journal);p.replay_statistics('backend','token','worker','E-TEST',journal)
   self.assertEqual(2,call.call_count);self.assertEqual(call.call_args_list[0],call.call_args_list[1]);self.assertEqual('2026-09-10T23:59:59+08:00',call.call_args.args[4]['statistics']['occurredAt'])
 @patch.object(p,'api_call')
 def test_filtered_event_does_not_create_failed_media(self,call):
  recorder=p.IncrementalVideoRecorder('backend','token','worker','E-TEST');recorder.finish([self.video('FILTERED','filtered-duration')])
  self.assertTrue(call.call_args.args[3].endswith('/statistics'))
 @patch.object(p,'api_call')
 def test_cache_and_success_metadata_share_video_transaction(self,call):
  p.record_video('backend','token','worker','E-TEST',self.video('CACHE'),download_status='DOWNLOADED',upload_status='PENDING')
  self.assertTrue(call.call_args.args[3].endswith('/videos'));self.assertEqual('CACHE',call.call_args.args[4]['statistics']['outcome'])
