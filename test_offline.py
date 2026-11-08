"""Offline checks. No credentials, sockets, OAuth, or live runtimes are used."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import settings
import _colab_local_gateway as gateway
import _colab_persist_drive as persist
import _colab_drive_cache as drive
import doctor
import release_check


class OfflineTests(unittest.TestCase):
    def test_defaults_and_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            defaults = settings.load_config(Path(folder) / 'missing.json')
        self.assertEqual(defaults, settings.DEFAULTS)
        self.assertEqual(defaults['gateway_port'], 11435)
        self.assertEqual(defaults['accounts'], ['token.account1.json'])
        self.assertEqual(defaults['config_dir'], '~/.config/colab-cli')
        self.assertEqual(defaults['proxy'], '')
        self.assertEqual(settings.project_path('state.txt'), settings.ROOT / 'state.txt')

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'gateway_port': 12345, 'proxy': 'http://localhost:9000', 'accounts': ['a.json']}))
            result = settings.load_config(path)
            self.assertEqual(result['gateway_port'], 12345)
            for value in ({'accounts': []}, {'gateway_port': True}, {'gateway_port': 0}, {'accounts': ['../token.json']}, {'unknown': 1}):
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    settings.load_config(path)

    def test_proxy_direct_and_explicit(self):
        with patch.dict(settings.CONFIG, {'proxy': ''}), patch.dict(os.environ, {'HTTP_PROXY': 'http://unwanted:1', 'ALL_PROXY': 'socks5://unwanted:2'}):
            settings.configure_proxy()
            self.assertNotIn('HTTP_PROXY', os.environ)
            self.assertNotIn('ALL_PROXY', os.environ)
            with patch.dict(settings.CONFIG, {'proxy': 'http://localhost:9000'}):
                settings.configure_proxy()
                self.assertEqual(os.environ['HTTPS_PROXY'], 'http://localhost:9000')
                self.assertIn('127.0.0.1', os.environ['NO_PROXY'])

    def test_credentials_proxy(self):
        for proxies in ({}, {'http': 'http://localhost:9000', 'https': 'http://localhost:9000'}):
            with patch.object(persist, 'PROXIES', proxies), patch.object(persist.Credentials, 'from_authorized_user_file') as loader, patch.object(persist, 'AuthorizedSession') as auth:
                loader.return_value.valid = False
                _, session = persist.credentials('token.account1.json')
                transport = loader.return_value.refresh.call_args.args[0].session
                self.assertFalse(transport.trust_env)
                self.assertEqual(transport.proxies, proxies)
                self.assertFalse(session.trust_env)
                self.assertEqual(session.proxies, proxies)

    def test_gateway_defaults_and_preservation(self):
        cases = [({}, 4096), ({'max_tokens': 5}, 5), ({'max_completion_tokens': 12}, None), ({'max_tokens': 0}, 0)]
        for payload, expected in cases:
            handler = object.__new__(gateway.ProxyHandler)
            body = json.dumps(payload).encode()
            handler.headers = {'Content-Length': str(len(body)), 'Content-Type': 'application/json'}
            handler.command = 'POST'
            handler.path = '/v1/chat/completions'
            handler.rfile = io.BytesIO(body)
            handler.wfile = io.BytesIO()
            handler.send_response = lambda *a: None
            handler.send_header = lambda *a: None
            handler.end_headers = lambda: None
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.headers = {}
            response.read.return_value = b''
            with patch.object(settings, 'get_api_key', return_value='offline-test-key'), patch.object(gateway, 'get_active_url', return_value='https://example.invalid'), patch.object(gateway.urllib.request, 'urlopen', return_value=response) as opener:
                handler._proxy()
                sent = json.loads(opener.call_args.args[0].data)
                self.assertEqual(sent.get('max_tokens'), expected)
                if 'max_completion_tokens' in payload:
                    self.assertEqual(sent['max_completion_tokens'], 12)

    def test_ping_has_no_deployment(self):
        source = settings.PING_SCRIPT.read_text()
        self.assertNotIn('subprocess', source)
        self.assertNotIn('_colab_one_shot.py', source)

    def test_optional_quota_header(self):
        for quota in ('', 'my-project'):
            with self.subTest(quota=quota), tempfile.TemporaryDirectory() as folder:
                session = unittest.mock.MagicMock()
                session.headers = {}
                session.__enter__.return_value = session
                remote = {'id': 'model', 'size': '1', 'md5Checksum': '0' * 32}
                with patch.object(drive.requests, 'Session', return_value=session), patch.object(drive, 'DRIVE_QUOTA_PROJECT', quota, create=True), patch.object(drive, 'find', side_effect=[[{'id': 'folder'}], [remote]]) as find, patch.object(drive, 'restore') as restore:
                    self.assertEqual(drive.cache_locked('synthetic-access', 'fixture.gguf', folder), remote)
                    self.assertEqual(session.headers.get('x-goog-user-project'), quota or None)
                    self.assertEqual(session.headers['Authorization'], 'Bearer synthetic-access')
                    find.assert_called()
                    restore.assert_called_once_with(session, remote, Path(folder) / 'fixture.gguf')

    def test_api_key_uses_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.txt'
            with patch.object(settings, 'API_KEY_FILE', path), patch.object(settings.secrets, 'token_urlsafe', return_value='a' * 64) as generate:
                first = settings.get_api_key()
                self.assertEqual(first, 'a' * 64)
                self.assertEqual(settings.get_api_key(), first)
                generate.assert_called_once_with(48)
                path.write_text('invalid key', encoding='ascii')
                with self.assertRaises(ValueError):
                    settings.get_api_key()
                self.assertEqual(path.read_text(encoding='ascii'), 'invalid key')

    def test_config_rejects_invalid_boundaries(self):
        values = [[], None, {'context_size': True}, {'context_size': 511},
                  {'context_size': 131073}, {'gateway_host': '0.0.0.0'},
                  {'proxy': 'socks5://localhost:1080'}, {'proxy': 'http://localhost'},
                  {'proxy': 'http://localhost:invalid'}, {'drive_owner_token': '../owner'},
                  {'accounts': 'owner.json'}, {'verification_targets': [{}]},
                  {'verification_targets': [{'url': 'file:///local', 'model': 'x'}]}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            for value in values:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        settings.load_config(path)
            for port, context in ((1, 512), (65535, 131072)):
                path.write_text(json.dumps({'gateway_port': port, 'context_size': context}), encoding='utf-8')
                self.assertEqual(settings.load_config(path)['context_size'], context)

    def test_doctor_malformed_credentials(self):
        values = [[], None, 7, 'fixture', {}, {'scopes': 'bad'},
                  {'refresh_token': 'fixture', 'client_id': 'fixture', 'client_secret': 'fixture',
                   'token_uri': 'https://example.invalid', 'scopes': [3]}]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'requirements.txt').write_text('', encoding='utf-8')
            token = root / 'fixture.json'
            with patch.object(doctor, 'ROOT', root), patch.object(doctor, 'CONFIG_DIR', root), patch.object(doctor, 'ACCOUNTS', ['fixture.json']), patch.object(doctor, 'DRIVE_OWNER_TOKEN', 'fixture.json'):
                for value in values:
                    with self.subTest(value=value):
                        token.write_text(json.dumps(value), encoding='utf-8')
                        results = doctor.checks(check_port=False)
                        credentials = [item for item in results if item['check'].startswith('credential:')]
                        self.assertEqual(len(credentials), 1)
                        self.assertFalse(credentials[0]['ok'])
                token.write_text('{', encoding='utf-8')
                self.assertFalse(next(item['ok'] for item in doctor.checks(False) if item['check'].startswith('credential:')))
                token.unlink()
                self.assertFalse(next(item['ok'] for item in doctor.checks(False) if item['check'].startswith('credential:')))

    def test_release_scanner_secret_detection_and_redaction(self):
        cases = {'access.txt': 'ya29.' + 'x' * 25,
                 'refresh.txt': '1/' + '/' + 'x' * 35,
                 'api.txt': 'AI' + 'za' + 'x' * 35,
                 'private.txt': '-----BEGIN ' + 'PRIVATE KEY-----',
                 'nested.json': json.dumps([{'nested': {'client_secret': 'synthetic-secret'}}]),
                 'oauth.json': json.dumps({'installed': {}}), 'malformed.json': '{',
                 'binary.txt': b'\xff\xfe', 'UPPER.KEY': 'fixture',
                 'runtime_api_key.txt': 'fixture', 'token.fixture.json': '{}'}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, content in cases.items():
                path = root / name
                path.write_bytes(content if isinstance(content, bytes) else content.encode())
            excluded = root / '.venv'
            excluded.mkdir()
            (excluded / 'token.hidden.json').write_text('{}')
            issues = release_check.scan(root)
            self.assertEqual(len(issues), len(cases))
            self.assertTrue(all(any(issue.startswith(name + ':') for issue in issues) for name in cases))
            self.assertNotIn('synthetic-secret', '\n'.join(issues))

    def test_release_scanner_accepts_own_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('release_check.py', 'test_offline.py', 'test_publish.py', 'config.example.json'):
                (root / name).write_bytes((settings.ROOT / name).read_bytes())
            self.assertEqual(release_check.scan(root), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
