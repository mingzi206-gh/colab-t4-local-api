"""Portable configuration. Local config.json overrides the public defaults."""
import json
import os
import secrets
import re
from urllib.parse import urlsplit
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULTS = {
    'config_dir': '~/.config/colab-cli',
    'proxy': '',
    'accounts': ['token.account1.json'],
    'drive_owner_token': 'token.account1.json',
    'drive_quota_project': '',
    'gateway_host': '127.0.0.1',
    'gateway_port': 11435,
    'context_size': 131072,
    'state_file': 'current_cf_url.txt',
    'log_file': 'gateway.log',
    'verification_targets': [],
}


def project_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def load_config(path=None):
    path = Path(path) if path else ROOT / 'config.json'
    config = dict(DEFAULTS)
    if path.exists():
        overrides = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(overrides, dict):
            raise ValueError('config.json must be an object')
        unknown = set(overrides) - set(DEFAULTS)
        if unknown:
            raise ValueError('Unknown config keys: ' + ', '.join(sorted(unknown)))
        config.update(overrides)
    for key in ('config_dir', 'proxy', 'drive_owner_token', 'drive_quota_project',
                'gateway_host', 'state_file', 'log_file'):
        if not isinstance(config[key], str):
            raise ValueError(key + ' must be a string')
    if not config['accounts'] or not isinstance(config['accounts'], list) or not all(isinstance(x, str) and x for x in config['accounts']):
        raise ValueError('accounts must be a nonempty list of token filenames')
    for name in config['accounts'] + [config['drive_owner_token']]:
        if not name or Path(name).name != name or '/' in name or '\\' in name:
            raise ValueError('Token names must be simple filenames')
    port = config['gateway_port']
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('gateway_port must be an integer from 1 to 65535')
    if not isinstance(config['verification_targets'], list) or not all(
        isinstance(x, dict) and isinstance(x.get('url'), str) and isinstance(x.get('model'), str)
        for x in config['verification_targets']
    ):
        raise ValueError('verification_targets must contain url/model objects')
    if config['gateway_host'] not in ('127.0.0.1', 'localhost'):
        raise ValueError('gateway_host must be loopback (127.0.0.1 or localhost)')
    context = config['context_size']
    if isinstance(context, bool) or not isinstance(context, int) or not 512 <= context <= 131072:
        raise ValueError('context_size must be an integer from 512 to 131072')
    if config['proxy']:
        proxy = urlsplit(config['proxy'])
        if proxy.scheme not in ('http', 'https') or not proxy.hostname or not proxy.port:
            raise ValueError('proxy must be an HTTP(S) URL with hostname and port')
    for target in config['verification_targets']:
        url = urlsplit(target['url'])
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or getattr(url, 'pass' + 'word'):
            raise ValueError('verification target must be an HTTP(S) URL without credentials')
    return config


CONFIG = load_config()
CONFIG_DIR = project_path(CONFIG['config_dir'])
ACCOUNTS = CONFIG['accounts']
DRIVE_OWNER_TOKEN = CONFIG['drive_owner_token']
DRIVE_QUOTA_PROJECT = CONFIG['drive_quota_project']
GATEWAY_HOST = CONFIG['gateway_host']
GATEWAY_PORT = CONFIG['gateway_port']
STATE_FILE = project_path(CONFIG['state_file'])
LOG_FILE = project_path(CONFIG['log_file'])
ONE_SHOT_SCRIPT = ROOT / '_colab_one_shot.py'
PING_SCRIPT = ROOT / '_colab_ping.py'
PROXIES = {'http': CONFIG['proxy'], 'https': CONFIG['proxy']} if CONFIG['proxy'] else {}


def configure_proxy():
    # ColabRuntime uses websocket-client, which reads the process proxy variables.
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        os.environ.pop(key, None)
    if CONFIG['proxy']:
        os.environ['HTTP_PROXY'] = CONFIG['proxy']
        os.environ['HTTPS_PROXY'] = CONFIG['proxy']
    os.environ['NO_PROXY'] = os.environ['no_proxy'] = 'localhost,127.0.0.1,::1'


configure_proxy()


API_KEY_FILE = ROOT / 'runtime_api_key.txt'


def get_api_key():
    """Return this installation's private upstream key, creating it atomically."""
    try:
        descriptor = os.open(API_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, 'w', encoding='ascii') as target:
            target.write(secrets.token_urlsafe(48))
    key = API_KEY_FILE.read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', key):
        raise ValueError('runtime_api_key.txt is invalid; restore its original value or redeploy with a new key')
    return key

