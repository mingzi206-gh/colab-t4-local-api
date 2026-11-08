"""Offline remote-runtime regression tests. No credentials, downloads or deployments."""
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import requests
import _colab_one_shot as runtime
import _colab_drive_cache as drive


def response(status=200, content=b'', payload=None, headers=None):
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    result.iter_content.return_value = iter([content])
    result.json.return_value = payload
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(response=result)
    return result


class DownloadTests(unittest.TestCase):
    def test_pinned_model_manifests_agree(self):
        for name in ('MODEL_NAME', 'MODEL_REVISION', 'MODEL_SHA256', 'MODEL_SIZE'):
            self.assertEqual(getattr(runtime, name), getattr(drive, name))
        self.assertEqual(len(runtime.MODEL_REVISION), 40)
        self.assertEqual(len(runtime.MODEL_SHA256), 64)

    def test_model_reuses_verified_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / runtime.MODEL_NAME
            model.write_bytes(b'model')
            with patch.object(runtime, 'MODEL_SIZE', 5), patch.object(runtime, 'MODEL_SHA256', hashlib.sha256(b'model').hexdigest()), patch.object(runtime, 'download') as download:
                self.assertEqual(runtime.ensure_model(directory), str(model))
                download.assert_not_called()

    def test_model_failed_download_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / runtime.MODEL_NAME
            model.write_bytes(b'old')
            def failed_download(url, target, checksum, size):
                Path(target).write_bytes(b'partial')
                raise RuntimeError('network interrupted')
            with patch.object(runtime, 'download', side_effect=failed_download):
                with self.assertRaisesRegex(RuntimeError, 'network interrupted'):
                    runtime.ensure_model(directory)
            self.assertEqual(model.read_bytes(), b'old')
            self.assertFalse(model.with_suffix('.gguf.part').exists())

    def test_model_download_atomically_replaces_invalid_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / runtime.MODEL_NAME
            model.write_bytes(b'old')
            def valid_download(url, target, checksum, size):
                self.assertIn('/resolve/' + runtime.MODEL_REVISION + '/', url)
                self.assertEqual(model.read_bytes(), b'old')
                Path(target).write_bytes(b'model')
            with patch.object(runtime, 'download', side_effect=valid_download):
                runtime.ensure_model(directory)
            self.assertEqual(model.read_bytes(), b'model')

    def test_verified_download_and_corruption_cleanup(self):

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'asset'
            with patch.object(runtime.requests, 'get', return_value=response(content=b'official')):
                runtime.download('https://example.invalid', target, hashlib.sha256(b'official').hexdigest(), 8)
            self.assertEqual(target.read_bytes(), b'official')
            with patch.object(runtime.requests, 'get', return_value=response(content=b'corrupt')):
                with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                    runtime.download('https://example.invalid', target, '0' * 64)
            self.assertFalse(target.exists())

    def test_tar_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'bad.tar'
            with tarfile.open(archive, 'w') as package:
                entry = tarfile.TarInfo('../escaped')
                entry.size = 1
                package.addfile(entry, io.BytesIO(b'x'))
            with self.assertRaises(tarfile.FilterError):
                runtime.extract_archive(archive, root / 'destination')
            self.assertFalse((root / 'escaped').exists())

    def test_missing_secret_fails_before_deployment(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runtime, 'ROOT', Path(directory)), patch.dict(runtime.os.environ, {}, clear=True), patch.object(runtime.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'REMOTE_API_KEY'):
                runtime.main()
            run.assert_not_called()

    def test_authentication_failure_stops_server_before_tunnel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / 'model.gguf'
            model.write_bytes(b'model')
            server = Mock()
            server.poll.return_value = None
            def fake_launch(command, logfile, records, env):
                records.append({'pid': 1234, 'starttime': 'test'})
                self.assertIn('--api-key-file', command)
                return server
            with patch.dict(runtime.os.environ, {'REMOTE_API_KEY': 'a' * 32}), patch.object(runtime, 'ROOT', root), patch.object(runtime, 'STATE', root / 'state.json'), patch.object(runtime, 'MODEL_SHA256', hashlib.sha256(b'model').hexdigest()), patch.object(runtime, 'MODEL_SIZE', 5), patch.object(runtime, 'stop_runtime'), patch.object(runtime, 'install_tools', return_value=(root, 'llama-server')), patch.object(runtime.subprocess, 'run'), patch.object(runtime, 'launch', side_effect=fake_launch) as launch, patch.object(runtime, 'stop_process') as stop, patch.object(runtime, 'ensure_model', return_value=str(model)), patch.object(runtime.requests, 'get', side_effect=[response(), response()]), patch.object(runtime.requests, 'post', return_value=response(payload={'choices': [{}]})) as warm:
                with self.assertRaisesRegex(RuntimeError, 'authentication check failed'):
                    runtime.main()
                self.assertEqual(launch.call_count, 1)
                stop.assert_called_once_with({'pid': 1234, 'starttime': 'test'})
                self.assertEqual(warm.call_args.kwargs['headers']['Authorization'], 'Bearer ' + 'a' * 32)
                self.assertFalse((root / '.inference-api-key').exists())
                self.assertFalse((root / 'cf_url.txt').exists())

    def test_reused_pid_is_never_stopped(self):
        with patch.object(runtime, 'process_identity', return_value='new'), patch.object(runtime.os, 'killpg', create=True) as kill:
            runtime.stop_process({'pid': 1234, 'starttime': 'old'})
            kill.assert_not_called()

    def test_missing_identity_is_never_stopped(self):
        with patch.object(runtime.os, 'killpg', create=True) as kill:
            with self.assertRaises(RuntimeError):
                runtime.stop_process({'pid': 1234, 'starttime': None})
            kill.assert_not_called()


class DriveTests(unittest.TestCase):
    def test_find_requests_newest_first(self):
        session = Mock()
        with patch.object(drive, 'request', return_value=response(payload={'files': []})) as request:
            self.assertEqual(drive.find(session, 'query'), [])
        self.assertEqual(request.call_args.kwargs['params']['orderBy'], 'modifiedTime desc')

    def test_duplicate_cache_skips_invalid_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / drive.MODEL_NAME
            local.write_bytes(b'model')
            valid = {'id': 'valid', 'size': '5', 'md5Checksum': hashlib.md5(b'model').hexdigest()}
            files = [{'id': 'bad-size', 'size': '3', 'md5Checksum': 'a' * 32},
                     {'id': 'bad-md5', 'size': '5', 'md5Checksum': 'bad'}, valid]
            session = Mock()
            session.__enter__ = Mock(return_value=session)
            session.__exit__ = Mock(return_value=False)
            session.headers = {}
            with patch.object(drive.requests, 'Session', return_value=session), patch.object(drive, 'find', side_effect=[[{'id': 'folder'}], files]), patch.object(drive, 'restore') as restore, patch.object(drive, 'MODEL_SIZE', 5), patch.object(drive, 'MODEL_SHA256', hashlib.sha256(b'model').hexdigest()):
                self.assertEqual(drive.cache_locked('test-token', drive.MODEL_NAME, directory), valid)
                restore.assert_called_once_with(session, valid, local)

    def test_permanent_http_errors_are_not_retried(self):
        session = Mock()
        session.request.return_value = response(401)
        with patch.object(drive.time, 'sleep') as sleep:
            with self.assertRaises(requests.HTTPError):
                drive.request(session, 'GET', 'https://example.invalid')
        self.assertEqual(session.request.call_count, 1)
        sleep.assert_not_called()

    def test_transient_get_is_retried_and_closed(self):
        session = Mock()
        failed = response(503)
        session.request.side_effect = [failed, response()]
        with patch.object(drive.time, 'sleep'):
            drive.request(session, 'GET', 'https://example.invalid')
        failed.close.assert_called_once()
        self.assertEqual(session.request.call_count, 2)

    def test_post_is_not_replayed(self):
        session = Mock()
        session.request.side_effect = requests.ConnectionError('lost response')
        with self.assertRaises(requests.ConnectionError):
            drive.request(session, 'POST', 'https://example.invalid')
        self.assertEqual(session.request.call_count, 1)

    def test_partial_download_retries_and_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'model.gguf'
            local.write_bytes(b'old')
            remote = {'id': 'id', 'size': '5', 'md5Checksum': hashlib.md5(b'model').hexdigest()}
            broken = response()
            broken.iter_content.side_effect = requests.ConnectionError('stream interrupted')
            with patch.object(drive, 'request', side_effect=[broken, response(content=b'model')]), patch.object(drive.time, 'sleep'):
                drive.restore(Mock(), remote, local)
            self.assertEqual(local.read_bytes(), b'model')
            self.assertFalse(local.with_suffix('.gguf.drive-part').exists())

    def test_drive_pinned_sha_failure_preserves_existing_model(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / drive.MODEL_NAME
            local.write_bytes(b'valid-old')
            remote = {'id': 'id', 'size': '5', 'md5Checksum': hashlib.md5(b'wrong').hexdigest()}
            with patch.object(drive, 'MODEL_SIZE', 5), patch.object(drive, 'MODEL_SHA256', hashlib.sha256(b'model').hexdigest()), patch.object(drive, 'request', side_effect=lambda *a, **k: response(content=b'wrong')), patch.object(drive.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'pinned model SHA256'):
                    drive.restore(Mock(), remote, local)
            self.assertEqual(local.read_bytes(), b'valid-old')
            self.assertFalse(local.with_suffix('.gguf.drive-part').exists())

    def test_corrupt_download_preserves_previous_model(self):

        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'model.gguf'
            local.write_bytes(b'old')
            remote = {'id': 'id', 'size': '5', 'md5Checksum': hashlib.md5(b'model').hexdigest()}
            with patch.object(drive, 'request', side_effect=lambda *a, **k: response(content=b'wrong')), patch.object(drive.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'checksum'):
                    drive.restore(Mock(), remote, local)
            self.assertEqual(local.read_bytes(), b'old')
            self.assertFalse(local.with_suffix('.gguf.drive-part').exists())

    def test_upload_no_progress_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'model.gguf'
            local.write_bytes(b'model')
            session = Mock()
            session.put.side_effect = lambda *a, **k: response(308)
            with patch.object(drive, 'request', return_value=response(headers={'Location': 'https://www.googleapis.com/upload/session'})), patch.object(drive.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'exhausted retries'):
                    drive.upload(session, local, 'model.gguf', 'folder')
            self.assertEqual(session.put.call_count, 5)

    def test_upload_recovers_repeated_transport_failure_with_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'model.gguf'
            local.write_bytes(b'model')
            remote = {'id': 'id', 'size': '5', 'md5Checksum': hashlib.md5(b'model').hexdigest()}
            session = Mock()
            session.put.side_effect = [requests.ConnectionError('lost'), requests.Timeout('probe lost'), response(308), response(payload=remote)]
            with patch.object(drive, 'request', return_value=response(headers={'Location': 'https://www.googleapis.com/upload/session'})), patch.object(drive.time, 'sleep'):
                self.assertEqual(drive.upload(session, local, 'model.gguf', 'folder'), remote)
            calls = session.put.call_args_list
            self.assertEqual(calls[1].kwargs['headers']['Content-Range'], 'bytes */5')
            self.assertEqual(calls[2].kwargs['headers']['Content-Range'], 'bytes */5')
            self.assertEqual(calls[3].kwargs['data'], b'model')

    def test_lock_always_released_on_failure(self):
        fake_fcntl = types.SimpleNamespace(LOCK_EX=1, LOCK_NB=4, LOCK_UN=8, flock=Mock())
        handle = io.StringIO()
        with patch.dict('sys.modules', {'fcntl': fake_fcntl}), patch('builtins.open', return_value=handle), patch.object(drive, 'cache_locked', side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                drive.cache_model('test-token', 'model.gguf')
        self.assertEqual(fake_fcntl.flock.call_args_list[-1].args[1], 8)
        self.assertTrue(handle.closed)

    def test_model_path_cannot_escape_cache(self):
        for filename in ['../model', '/model', 'dir/model', '..', 'a\\b']:
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                drive.cache_model('test-token', filename)


if __name__ == '__main__':
    unittest.main()
