#!/usr/bin/env python3
"""Persist an allowlisted capture restriction using the trusted server registry."""
import json
import os
import sys
from pathlib import Path
from hm_facebook_account import capture_restriction

if __name__ == '__main__':
    try:
        tenant = os.environ['HM_CAPTURE_TENANT']
        config = json.loads(Path(os.environ['HM_TENANT_CONFIG']).read_text())[tenant]
        request = json.load(sys.stdin)
        capture_restriction(config, tenant, request['code'], int(request.get('retrySeconds') or 1800))
        print('{"persisted":true}')
    except Exception:
        print('{"persisted":false}')
        sys.exit(1)
