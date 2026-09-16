import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import hm_facebook_account as accounts
from facebook_video_ingest import manifest_error_code

class PhaseOneTests(unittest.TestCase):
    def test_account_restriction_survives_partial_success(self):
        payload = {'sources': [{'errorCode': 'FACEBOOK_RATE_LIMITED', 'videos': [{'status': 'downloaded'}]}]}
        self.assertEqual(manifest_error_code(payload), 'FACEBOOK_RATE_LIMITED')
        payload['sources'][0]['errorCode'] = 'FACEBOOK_ACCESS_REQUIRED'
        self.assertEqual(manifest_error_code(payload), 'FACEBOOK_ITEM_FAILURE')

    def test_restriction_is_durable_when_callback_fails_and_stale_probe_cannot_clear_it(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'HM_FACEBOOK_ACCOUNT_ROOT': temp}), patch.object(accounts, 'report', side_effect=OSError('offline')):
            config = {'facebookAccount': {'key': 'shared-account'}}
            account = config['facebookAccount']
            (accounts.account_root(account) / 'state.json').write_text(json.dumps({'state': 'AVAILABLE', 'version': 1}))
            current = accounts.capture_restriction(config, 'ph', 'FACEBOOK_RATE_LIMITED', 3600)
            self.assertEqual(accounts.read_state(account)['state'], 'COOLDOWN')
            self.assertGreaterEqual(current['nextCheckAt'] - current['checkedAt'], 3600)
            stale = accounts.save_state(account, 'AVAILABLE', None, observed_version=1)
            self.assertEqual(stale, current)
            # Only a fresh serialized probe may reopen access after cooldown.
            recovered = accounts.save_state(account, 'AVAILABLE', None, observed_version=current['version'])
            self.assertEqual(recovered['state'], 'AVAILABLE')

    def test_rate_limit_does_not_downgrade_verification(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'HM_FACEBOOK_ACCOUNT_ROOT': temp}), patch.object(accounts, 'report'):
            config = {'facebookAccount': {'key': 'shared-account'}}
            account = config['facebookAccount']
            (accounts.account_root(account) / 'state.json').write_text(json.dumps({'state': 'VERIFICATION_REQUIRED', 'version': 7}))
            self.assertEqual(accounts.capture_restriction(config, 'ph', 'FACEBOOK_RATE_LIMITED')['state'], 'VERIFICATION_REQUIRED')

    def test_metrics_summary_keeps_empty_checks_and_skips_truncated_lines(self):
        from summarize_capture_metrics import summarize
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = {'schemaVersion': 1, 'attemptId': 'test', 'stage': 'no_new_check', 'count': 1, 'elapsedMs': 0, 'outcome': 'count'}
            (root / 'process-metrics.jsonl').write_text('{"truncated":\n' + json.dumps(row) + '\n')
            result = summarize([root, root])
            self.assertEqual(result['attempts'], 1)
            self.assertEqual(result['invalidLines'], 1)
            self.assertEqual(result['stages']['no_new_check']['count'], 1)

if __name__ == '__main__':
    unittest.main()
