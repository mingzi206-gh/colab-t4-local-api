"""Conservative source release scan. No network or credential values are printed."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXCLUDED_DIRS = {'.git', '.venv', 'venv', '__pycache__'}
PRIVATE_NAMES = {'config.json', 'runtime_api_key.txt', 'current_cf_url.txt', 'gateway.log'}


def contains_credentials(value):
    if isinstance(value, dict):
        if any(value.get(key) for key in ('refresh_token', 'access_token', 'client_secret', 'private_key')):
            return True
        if 'installed' in value or 'web' in value:
            return True
        return any(contains_credentials(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_credentials(item) for item in value)
    return False


def scan(root=ROOT):
    problems = []
    for path in root.rglob('*'):
        if not path.is_file() or any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts):
            continue
        rel = str(path.relative_to(root))
        if path.name.lower() in PRIVATE_NAMES or path.name.lower().startswith('token.') or path.suffix.lower() in {'.pem', '.key', '.gguf', '.log'}:
            problems.append(rel + ': private/runtime artifact must not be released')
            continue
        if path.suffix.lower() not in {'.py', '.json', '.md', '.txt', '.yml', '.yaml', '.ps1'}:
            continue
        try:
            text = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError):
            problems.append(rel + ': unreadable source file')
            continue
        if re.search(r'ya29\.[A-Za-z0-9_-]{20,}|1//[A-Za-z0-9_-]{30,}|AIza[A-Za-z0-9_-]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', text):
            problems.append(rel + ': possible secret material')
        if path.suffix.lower() == '.json':
            try:
                value = json.loads(text)
                if contains_credentials(value):
                    problems.append(rel + ': OAuth credentials/client configuration must not be released')
            except ValueError:
                problems.append(rel + ': invalid JSON')
    return problems


def main():
    issues = scan()
    for issue in issues:
        print('FAIL ' + issue)
    if not issues:
        print('PASS source release scan (not a substitute for staged diff review)')
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
