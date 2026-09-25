"""CI runner with compact, visible totals. Does not suppress test failures."""
import json
import os
from pathlib import Path
import sys
import unittest


def main():
    native = '--native' in sys.argv
    names = ['test_windows_smoke'] if native else [
        path.stem for path in sorted(Path('.').glob('test_*.py'))
        if path.stem != 'test_windows_smoke'
    ]
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    summary = {'tests': result.testsRun, 'failures': len(result.failures),
               'errors': len(result.errors), 'skipped': len(result.skipped),
               'successful': result.wasSuccessful()}
    print('TEST SUMMARY: ' + json.dumps(summary), flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
            handle.write('### Test result\n```json\n' + json.dumps(summary, indent=2) + '\n```\n')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
