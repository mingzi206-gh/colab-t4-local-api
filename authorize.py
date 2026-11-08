"""Authorize your own Desktop OAuth client for Colab and app-created Drive files."""
import argparse
import json
import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from colab_cli.auth import PUBLIC_SCOPES
from settings import CONFIG_DIR, ACCOUNTS, CONFIG, PROXIES, project_path

SCOPES = list(dict.fromkeys([*PUBLIC_SCOPES, 'https://www.googleapis.com/auth/drive.file']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-secrets', required=True, help='Your Desktop OAuth client JSON file')
    parser.add_argument('--token', default=ACCOUNTS[0], help='Token filename in configured config_dir')
    args = parser.parse_args()
    if not args.token or Path(args.token).name != args.token or '/' in args.token or '\\' in args.token:
        parser.error('--token must be a simple filename')
    client_path = project_path(args.client_secrets)
    client = json.loads(client_path.read_text(encoding='utf-8-sig'))
    if 'installed' not in client:
        parser.error('--client-secrets must contain a Desktop OAuth client (installed)')
    flow = InstalledAppFlow.from_client_config(client, scopes=SCOPES)
    flow.oauth2session.trust_env = False
    flow.oauth2session.proxies = dict(PROXIES)
    # OAuthlib otherwise rejects accounts that already granted additional scopes.
    os.environ.setdefault('OAUTHLIB_RELAX_TOKEN_SCOPE', '1')
    credentials = flow.run_local_server(host='localhost', port=0, access_type='offline', prompt='consent')
    if not credentials.refresh_token:
        raise RuntimeError('No refresh token returned; grant consent for offline access')
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    target = CONFIG_DIR / args.token
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(credentials.to_json(), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(target)
    print('Saved token to ' + str(target))


if __name__ == '__main__':
    main()
