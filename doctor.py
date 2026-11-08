"""Read-only deployment preflight. Never authorizes, allocates a VM or prints credentials."""
import argparse
import importlib.metadata
import json
import socket
import sys
from pathlib import Path

from settings import ACCOUNTS, CONFIG, CONFIG_DIR, DRIVE_OWNER_TOKEN, GATEWAY_HOST, GATEWAY_PORT, ROOT, PROXIES


def checks(check_port=True):
    results = []
    def add(name, ok, detail):
        results.append({'check': name, 'ok': bool(ok), 'detail': detail})
    add('python', sys.version_info >= (3, 12), 'Requires Python >=3.12; reference environment is 3.13')
    for line in (ROOT / 'requirements.txt').read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        name, expected = line.split('==', 1)
        try:
            actual = importlib.metadata.version(name)
            add('dependency:' + name, actual == expected, 'installed=' + actual + '; expected=' + expected)
        except importlib.metadata.PackageNotFoundError:
            add('dependency:' + name, False, 'not installed')
    for name in dict.fromkeys([*ACCOUNTS, DRIVE_OWNER_TOKEN]):
        path = CONFIG_DIR / name
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            valid = isinstance(data, dict) and all(data.get(k) for k in ('refresh_token', 'client_id', 'client_secret', 'token_uri'))
            scopes = data.get('scopes', []) if isinstance(data, dict) else []
            valid = valid and isinstance(scopes, list) and all(isinstance(s, str) for s in scopes) and 'https://www.googleapis.com/auth/colaboratory' in scopes and 'https://www.googleapis.com/auth/drive.file' in scopes
            add('credential:' + name, valid, 'required fields/scopes present' if valid else 'required fields or scopes missing; reauthorize')
        except (OSError, ValueError, TypeError):
            add('credential:' + name, False, 'missing/unreadable or invalid JSON; complete owner OAuth')
    add('drive_quota_project', bool(CONFIG['drive_quota_project'].strip()), 'set your Drive-enabled Google Cloud quota project; IAM/API availability is not checked offline')
    add('remote_source', all((ROOT / n).is_file() for n in ('_colab_one_shot.py', '_colab_drive_cache.py', '_colab_ping.py')), 'required scripts exist')
    if check_port:
        try:
            with socket.socket() as listener:
                listener.bind((GATEWAY_HOST, GATEWAY_PORT))
            add('port', True, 'local gateway port is available')
        except OSError:
            add('port', False, 'local gateway port unavailable; inspect existing process, do not kill it blindly')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--skip-port', action='store_true', help='Use when checking an intentionally running gateway')
    args = parser.parse_args()
    results = checks(not args.skip_port)
    passed = all(r['ok'] for r in results)
    if args.json:
        print(json.dumps({'ok': passed, 'checks': results, 'cloud_verified': False}, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(('PASS' if result['ok'] else 'FAIL') + ' ' + result['check'] + ': ' + result['detail'])
        print('Offline preflight only. No OAuth refresh, cloud resource allocation or network test performed.')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
