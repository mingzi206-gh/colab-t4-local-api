"""Offline tests for publishing existing runtimes; no credentials or cloud calls."""
import base64
import contextlib
import io
import types
import unittest
from unittest.mock import Mock, patch

import _colab_local_gateway as gateway
import _colab_publish_existing as publish


class PublishTests(unittest.TestCase):
    def assignment(self):
        return types.SimpleNamespace(runtime_proxy_info=types.SimpleNamespace(
            url='https://runtime.example.invalid/', token='synthetic-proxy-token'))

    def session(self, content, format='text'):
        session = Mock()
        session.get.return_value.json.return_value = {'content': content, 'format': format}
        return session

    def test_plain_and_base64_publish_only_after_health(self):
        url = 'https://fixture.trycloudflare.com'
        for content, format in ((url + '\n', 'text'), (base64.b64encode(url.encode()).decode(), 'base64')):
            with self.subTest(format=format):
                session = self.session(content, format)
                events = []
                with patch.object(publish, 'existing_runtimes', return_value=iter([('fixture', session, self.assignment())])), patch.object(gateway, 'check_tunnel_health', side_effect=lambda value: events.append(('health', value)) or True), patch.object(gateway, 'set_active_url', side_effect=lambda value: events.append(('publish', value))), contextlib.redirect_stdout(io.StringIO()):
                    publish.main()
                self.assertEqual(events, [('health', url), ('publish', url)])
                session.get.return_value.raise_for_status.assert_called_once()
                self.assertEqual(session.get.call_args.kwargs['timeout'], 30)

    def test_rejects_invalid_url_encoding_and_unhealthy_tunnel(self):
        cases = [('http://fixture.trycloudflare.com', 'text', True),
                 ('https://example.invalid', 'text', True),
                 ('https://fixture.trycloudflare.com/extra', 'text', True),
                 ('not valid base64!', 'base64', True),
                 ('https://fixture.trycloudflare.com', 'text', False)]
        for content, format, healthy in cases:
            with self.subTest(content=content):
                session = self.session(content, format)
                with patch.object(publish, 'existing_runtimes', return_value=iter([('fixture', session, self.assignment())])), patch.object(gateway, 'check_tunnel_health', return_value=healthy), patch.object(gateway, 'set_active_url') as write, contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError, 'No healthy existing'):
                        publish.main()
                    write.assert_not_called()

    def test_failed_runtime_falls_through_to_next_existing(self):
        bad = self.session('unavailable')
        bad.get.side_effect = TimeoutError('synthetic timeout')
        good = self.session('https://healthy.trycloudflare.com')
        assignments = [('first', bad, self.assignment()), ('second', good, self.assignment())]
        with patch.object(publish, 'existing_runtimes', return_value=iter(assignments)), patch.object(gateway, 'check_tunnel_health', return_value=True), patch.object(gateway, 'set_active_url') as write, contextlib.redirect_stdout(io.StringIO()):
            publish.main()
            write.assert_called_once_with('https://healthy.trycloudflare.com')

    def test_no_runtime_does_not_publish(self):
        with patch.object(publish, 'existing_runtimes', return_value=iter([])), patch.object(gateway, 'set_active_url') as write:
            with self.assertRaisesRegex(RuntimeError, 'No healthy existing'):
                publish.main()
            write.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
