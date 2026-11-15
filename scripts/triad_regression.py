"""Run the entire test repository and persist actual counts and missing inputs."""
import argparse
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'server')]


class ObservedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = []

    def addSuccess(self, test):
        self.passed.append(test.id())
        super().addSuccess(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    with (args.evidence / 'full-regression.log').open('w', encoding='utf8') as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=ObservedResult).run(suite)
    record = dict(command=sys.argv, python=sys.version, root=str(ROOT), seconds=time.perf_counter()-started,
                  tests_run=result.testsRun, passed=len(result.passed), failed=len(result.failures),
                  errors=len(result.errors), skipped=len(result.skipped),
                  skipped_tests=[{'test':t.id(),'reason':reason} for t,reason in result.skipped],
                  failures=[{'test':t.id(),'traceback':reason} for t,reason in result.failures],
                  error_tests=[{'test':t.id(),'traceback':reason} for t,reason in result.errors],
                  exit_code=0 if result.wasSuccessful() else 1,
                  limitation='Skipped real-data and other prerequisite-dependent tests are NOT verified.')
    (args.evidence / 'full-regression.json').write_text(json.dumps(record, ensure_ascii=False, indent=2),encoding='utf8')
    print(json.dumps({k:v for k,v in record.items() if k not in ['skipped_tests','failures','error_tests']},ensure_ascii=False))
    return record['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
