#!/usr/bin/env python3
"""Summarize first-phase capture operation metrics from explicit execution directories."""
import argparse
import json
from pathlib import Path


def summarize(roots):
    stages, attempts, files = {}, set(), set()
    invalid = 0
    for root in roots:
        for file in root.rglob('process-metrics.jsonl'):
            if file.resolve() in files:
                continue
            files.add(file.resolve())
            for line in file.read_text().splitlines():
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError('Invalid metric record')
                    if row.get('schemaVersion') != 1:
                        continue
                    count, elapsed = int(row['count']), int(row['elapsedMs'])
                    if count < 0 or elapsed < 0:
                        raise ValueError('Negative counter')
                    attempts.add(row['attemptId'])
                    total = stages.setdefault(row['stage'], {'count': 0, 'failures': 0, 'elapsedMs': 0})
                    total['count'] += count
                    total['elapsedMs'] += elapsed
                    total['failures'] += row['outcome'] == 'failure'
                except (ValueError, KeyError, TypeError):
                    invalid += 1
    return {'attempts': len(attempts), 'files': len(files), 'invalidLines': invalid, 'stages': stages}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('executions', type=Path, nargs='+')
    args = parser.parse_args()
    print(json.dumps(summarize(args.executions), ensure_ascii=False, indent=2))
