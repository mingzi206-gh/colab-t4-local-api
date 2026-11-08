"""Offline lifecycle regression tests; never contact OAuth, Drive or Colab."""
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import _colab_local_gateway as gateway
import _colab_persist_drive as persist


class LifecycleTests(unittest.TestCase):
    def test_script_main_and_secret_cleanup(self):
        outer = {'__name__': 'not_main', 'result': []}
        code = "DRIVE_ACCESS_TOKEN='test-secret'\nif __name__ == '__main__':\n    print('SCRIPT_RAN')\n"
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            exec(persist.script_code(code), outer)
        self.assertIn('SCRIPT_RAN', output.getvalue())
        self.assertNotIn('DRIVE_ACCESS_TOKEN', outer)
        self.assertNotIn('__colab_run_script', outer)

    def test_script_cleanup_on_error(self):
        outer = {'__name__': 'not_main'}
        with self.assertRaises(ValueError):
            exec(persist.script_code("SECRET='hidden'\nraise ValueError('fail')"), outer)
        self.assertNotIn('SECRET', outer)
        self.assertNotIn('__colab_run_script', outer)

    def test_busy_vm_rejected_even_with_idle_second_kernel(self):
        session = MagicMock()
        session.get.return_value.json.return_value = [
            {'id': 'busy', 'execution_state': 'busy'}, {'id': 'idle', 'execution_state': 'idle'}]
        with self.assertRaises(persist.RuntimeBusyError):
            persist.idle_kernel(session, 'https://runtime', 'test-token')

    def test_each_vm_discovers_own_kernel_and_timeout_closes_without_shutdown(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(persist, 'ROOT', Path(folder)), \
             patch.object(persist, 'ColabRuntime') as runtime:
            session = MagicMock()
            session.get.return_value.json.side_effect = [
                [{'id': 'vm-one-kernel', 'execution_state': 'idle'}],
                [{'id': 'vm-two-kernel', 'execution_state': 'idle'}]]
            runtime.return_value.execute_code.return_value = [{'text': 'ok'}]
            persist.execute_script(session, 'https://one', 'token1', 'print(1)')
            runtime.return_value.execute_code.side_effect = TimeoutError('quiet but still running')
            with self.assertRaises(persist.RemoteExecutionError):
                persist.execute_script(session, 'https://two', 'token2', 'print(2)')
            self.assertEqual([c.kwargs['kernel_id'] for c in runtime.call_args_list], ['vm-one-kernel', 'vm-two-kernel'])
            self.assertFalse(runtime.return_value.kernel_client.execute_interactive.keywords['store_history'])
            runtime.return_value.stop.assert_called_with(shutdown_kernel=False)

    def test_health_rejects_html_and_loading(self):
        for body in (b'<html>OK</html>', b'{"status":"loading model"}'):
            response = MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.read.return_value = body
            with patch.object(gateway.urllib.request, 'urlopen', return_value=response):
                self.assertFalse(gateway.check_tunnel_health('https://test.trycloudflare.com'))

    def test_atomic_url_state(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(gateway, 'STATE_FILE', Path(folder) / 'state'):
            gateway.set_active_url('https://test.trycloudflare.com/')
            self.assertEqual(gateway.get_active_url(), 'https://test.trycloudflare.com')
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def handler(self, method='POST', path='/v1/chat/completions', body=b'{}', headers=None):
        handler = object.__new__(gateway.ProxyHandler)
        handler.command, handler.path = method, path
        handler.headers = headers or {'Content-Length': str(len(body)), 'Content-Type': 'application/json'}
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = MagicMock(), MagicMock(), MagicMock()
        return handler

    def test_post_never_retried(self):
        handler = self.handler()
        with patch.object(gateway, 'get_active_url', return_value='https://test.trycloudflare.com'), \
             patch.object(gateway.settings, 'get_api_key', return_value='installation-test-key'), \
             patch.object(gateway.urllib.request, 'urlopen', side_effect=TimeoutError) as opener:
            handler._proxy()
        self.assertEqual(opener.call_count, 1)
        handler.send_response.assert_called_once_with(502)

    def test_stream_uses_read1_and_does_not_send_second_response(self):
        handler = self.handler()
        response = MagicMock()
        response.__enter__.return_value = response
        response.status, response.headers = 200, {'Content-Type': 'text/event-stream'}
        response.read1.side_effect = [b'data: first\n\n', TimeoutError()]
        with patch.object(gateway, 'get_active_url', return_value='https://test.trycloudflare.com'), \
             patch.object(gateway.settings, 'get_api_key', return_value='installation-test-key'), \
             patch.object(gateway.urllib.request, 'urlopen', return_value=response) as opener:
            handler._proxy()
        self.assertEqual(opener.call_count, 1)
        response.read.assert_not_called()
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(handler.wfile.getvalue(), b'data: first\n\n')
        self.assertEqual(opener.call_args.args[0].get_header('Authorization'), 'Bearer installation-test-key')

    def test_private_routes_and_invalid_lengths_never_reach_upstream(self):
        cases = [('/props', {}), ('/v1/chat/completions', {'Content-Length': '-1'}),
                 ('/v1/chat/completions', {'Content-Length': '999999999'}),
                 ('/v1/chat/completions', {'Transfer-Encoding': 'chunked'})]
        for path, headers in cases:
            handler = self.handler(path=path, headers=headers)
            with patch.object(gateway.urllib.request, 'urlopen') as opener:
                handler._proxy()
            opener.assert_not_called()
            self.assertIn(handler.send_response.call_args.args[0], (400, 404, 413))

    def test_watchdog_survives_state_error_and_is_stoppable(self):
        stop = MagicMock()
        stop.is_set.side_effect = [False, True]
        with patch.object(gateway, 'get_active_url', side_effect=OSError('state read failure')):
            gateway.watchdog_loop(stop)
        stop.wait.assert_called_once_with(15)

    def test_busy_account_falls_through_and_no_assignment_is_deleted(self):
        proxy = SimpleNamespace(url='https://runtime', token='test-token')
        client = MagicMock()
        client.session.get.return_value.status_code = 200
        client.session.get.return_value.json.return_value = {
            'content': '{"project":"colab-replica"}', 'format': 'text'}
        client.list_assignments.return_value = [SimpleNamespace(runtime_proxy_info=proxy, accelerator=gateway.Accelerator.T4)]
        with patch.object(gateway, 'ACCOUNTS', ['first', 'second']), \
             patch.object(gateway, 'get_client_for_account', return_value=client) as lookup, \
             patch.object(gateway, 'idle_kernel', side_effect=persist.RuntimeBusyError()):
            self.assertEqual(gateway.deploy_on_next_available_account(), '')
        self.assertEqual(lookup.call_count, 2)
        client.unassign.assert_not_called()
        client.assign.assert_not_called()

    def test_stop_existing_preserves_arguments_and_never_needs_key(self):
        import _colab_stop_existing as stop
        import sys
        assignment = SimpleNamespace(runtime_proxy_info=SimpleNamespace(url='https://runtime', token='test-token'))
        original_argv = sys.argv
        observed = []
        fake_script = MagicMock()
        fake_script.read_text.return_value = "import sys\nif __name__ == '__main__':\n    assert sys.argv == ['remote', '--stop']\n"
        def execute(session, url, token, source, **kwargs):
            exec(persist.script_code(source), {})
            observed.append(url)
        with patch.object(stop, 'ONE_SHOT_SCRIPT', fake_script), \
             patch.object(stop, 'existing_runtimes', return_value=iter([('first', object(), assignment)])), \
             patch.object(stop, 'execute_script', side_effect=execute), \
             patch.object(gateway.settings, 'get_api_key') as key:
            stop.main()
        self.assertIs(sys.argv, original_argv)
        self.assertEqual(observed, ['https://runtime'])
        key.assert_not_called()

    def test_stop_no_project_runtime_does_not_execute(self):
        import _colab_stop_existing as stop
        with patch.object(stop, 'existing_runtimes', return_value=iter([])), \
             patch.object(stop, 'execute_script') as execute:
            with self.assertRaisesRegex(RuntimeError, 'No project runtime found'):
                stop.main()
        execute.assert_not_called()

    def test_existing_runtime_generator_excludes_unrelated_assignments(self):
        session = MagicMock()
        assignment = SimpleNamespace(accelerator=gateway.Accelerator.T4,
            runtime_proxy_info=SimpleNamespace(url='https://runtime', token='test-token'))
        session.get.return_value.status_code = 404
        with patch.object(persist, 'ACCOUNTS', ['first']), \
             patch.object(persist, 'credentials', return_value=(None, session)), \
             patch.object(persist, 'Client') as client:
            client.return_value.list_assignments.return_value = [assignment]
            self.assertEqual(list(persist.existing_runtimes()), [])
        client.return_value.assign.assert_not_called()

    def test_ownership_requires_valid_marker_content(self):
        assignment = SimpleNamespace(accelerator=gateway.Accelerator.T4,
            runtime_proxy_info=SimpleNamespace(url='https://runtime', token='test-token'))
        session = MagicMock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {'content': '{}', 'format': 'text'}
        self.assertFalse(persist.is_owned_runtime(session, assignment))
        session.get.return_value.json.return_value = {
            'content': '{"project":"colab-replica"}', 'format': 'text'}
        self.assertTrue(persist.is_owned_runtime(session, assignment))
        assignment.accelerator = 'NONE'
        session.get.reset_mock()
        self.assertFalse(persist.is_owned_runtime(session, assignment))
        session.get.assert_not_called()

    def test_bind_failure_never_starts_watchdog(self):
        with patch.object(gateway, 'ThreadedHTTPServer', side_effect=OSError('address in use')), \
             patch.object(gateway.threading, 'Thread') as thread:
            with self.assertRaises(OSError):
                gateway.main()
        thread.assert_not_called()

    def test_healthy_watchdog_never_executes_keepalive(self):
        stop = MagicMock()
        stop.is_set.side_effect = [False, True]
        with patch.object(gateway, 'get_active_url', return_value='https://test.trycloudflare.com'), \
             patch.object(gateway, 'check_tunnel_health', return_value=True), \
             patch.object(gateway, 'run_code_on_vm') as execute, \
             patch.object(gateway, 'get_client_for_account') as client:
            gateway.watchdog_loop(stop)
        execute.assert_not_called()
        client.assert_not_called()
        stop.wait.assert_called_once_with(90)

    def test_existing_cpu_assignment_is_never_modified(self):
        client = MagicMock()
        client.list_assignments.return_value = [SimpleNamespace(accelerator='NONE')]
        with patch.object(gateway, 'get_client_for_account', return_value=client), \
             patch.object(gateway, 'run_code_on_vm') as execute:
            self.assertEqual(gateway.deploy_on_next_available_account(), '')
        execute.assert_not_called()
        client.assign.assert_not_called()

    def test_publish_import_is_offline(self):
        import importlib
        with patch.object(persist, 'credentials') as creds:
            module = importlib.import_module('_colab_publish_existing')
            importlib.reload(module)
        creds.assert_not_called()

    def test_submitted_timeout_does_not_deploy_other_accounts(self):
        proxy = SimpleNamespace(url='https://runtime', token='test-token')
        client = MagicMock()
        client.session.get.return_value.status_code = 200
        client.session.get.return_value.json.return_value = {
            'content': '{"project":"colab-replica"}', 'format': 'text'}
        client.list_assignments.return_value = [SimpleNamespace(runtime_proxy_info=proxy, accelerator=gateway.Accelerator.T4)]
        with patch.object(gateway, 'ACCOUNTS', ['first', 'second']), \
             patch.object(gateway, 'get_client_for_account', return_value=client) as lookup, \
             patch.object(gateway, 'idle_kernel'), patch.object(gateway, 'deployment_source', return_value='pass'), \
             patch.object(gateway, 'run_code_on_vm', side_effect=persist.RemoteExecutionError('timeout')):
            self.assertEqual(gateway.deploy_on_next_available_account(), '')
        self.assertEqual(lookup.call_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
