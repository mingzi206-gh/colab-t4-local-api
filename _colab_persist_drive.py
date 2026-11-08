"""Archive the active model to the owner's Google Drive from an existing Colab VM."""
import contextlib
import functools
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from settings import ROOT, CONFIG_DIR, DRIVE_OWNER_TOKEN, DRIVE_QUOTA_PROJECT, ACCOUNTS, PROXIES
import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request, AuthorizedSession
from colab_cli.client import Accelerator, Client, Prod
from colab_cli.runtime import ColabRuntime

CONFIG = CONFIG_DIR
PROJECT = DRIVE_QUOTA_PROJECT
MODEL = 'DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.i1-Q4_K_M.gguf'
_execution_lock = threading.RLock()


class RuntimeBusyError(RuntimeError):
    pass


class RemoteExecutionError(RuntimeError):
    """Execution was submitted; never retry it on another account automatically."""


def credentials(name):
    transport = requests.Session()
    transport.trust_env = False
    transport.proxies = dict(PROXIES)
    creds = Credentials.from_authorized_user_file(str(CONFIG / name))
    if not creds.valid:
        creds.refresh(Request(session=transport))
    session = AuthorizedSession(creds, auth_request=Request(session=transport))
    session.trust_env = False
    session.proxies = dict(PROXIES)
    return creds, session


@contextlib.contextmanager
def runtime_lock(url):
    """Serialize our CLI processes as well as watchdog threads for the same VM."""
    digest = hashlib.sha256(url.rstrip('/').encode()).hexdigest()[:24]
    lock_dir = ROOT / '.runtime-locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    with _execution_lock, (lock_dir / (digest + '.lock')).open('a+b') as handle:
        handle.seek(0)
        if handle.read(1) == b'':
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeBusyError('Another local process is using this runtime') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def idle_kernel(session, url, token):
    response = session.get(url.rstrip('/') + '/api/kernels',
        params={'colab-runtime-proxy-token': token},
        headers={'X-Colab-Runtime-Proxy-Token': token}, timeout=30)
    response.raise_for_status()
    kernels = response.json()
    if not isinstance(kernels, list):
        raise RuntimeError('Invalid kernel listing')
    # Even an idle second kernel must not race a long model setup in a busy kernel.
    if any(k.get('execution_state') != 'idle' for k in kernels):
        raise RuntimeBusyError('Existing runtime is busy; retry later')
    return kernels[0]['id'] if kernels else None


def script_code(source, filename='<remote-script>'):
    """Run script guards reliably without polluting a notebook's global namespace."""
    return ('def __colab_run_script():\n'
            '    namespace = {"__name__": "__main__", "__file__": ' + repr(filename) + '}\n'
            '    try:\n'
            '        exec(compile(' + repr(source) + ', ' + repr(filename) + ', "exec"), namespace)\n'
            '    finally:\n'
            '        namespace.clear()\n'
            'try:\n'
            '    __colab_run_script()\n'
            'finally:\n'
            '    del __colab_run_script\n')


def execute_script(session, url, token, source, timeout=1800, filename='<remote-script>'):
    with runtime_lock(url):
        kernel_id = idle_kernel(session, url, token)
        runtime = ColabRuntime(url, token, kernel_id=kernel_id)
        try:
            # ColabRuntime exposes no store_history option. Its interactive delegate does.
            # Bind only this connection so embedded short-lived tokens never enter IPython history.
            kernel_client = runtime.kernel_client
            kernel_client.execute_interactive = functools.partial(
                kernel_client.execute_interactive, store_history=False)
            outputs = runtime.execute_code(script_code(source, filename), timeout=timeout,
                output_hook=lambda out: print(out.get('text', ''), end='', flush=True)
                if out.get('output_type') == 'stream' else None)
            errors = [out for out in outputs if out.get('output_type') == 'error']
            if errors:
                # Tracebacks can contain the embedded access token. Do not echo source.
                raise RemoteExecutionError('Remote script failed: ' + ', '.join(out.get('ename', 'Error') for out in errors))
            return '\n'.join(''.join(out['text']) if isinstance(out['text'], list) else out['text']
                             for out in outputs if 'text' in out)
        except RemoteExecutionError:
            raise
        except Exception as exc:
            raise RemoteExecutionError('Remote execution did not complete (' + type(exc).__name__ +
                                       '); the kernel may still be running') from exc
        finally:
            runtime.stop(shutdown_kernel=False)


def is_owned_runtime(session, assignment):
    """Only this project's T4 assignments may be modified by unattended commands."""
    if getattr(assignment, 'accelerator', None) != Accelerator.T4:
        return False
    proxy = assignment.runtime_proxy_info
    for filename in ('colab-replica-owner.json', 'colab-runtime.json'):
        response = session.get(proxy.url.rstrip('/') + '/api/contents/content/' + filename,
            params={'colab-runtime-proxy-token': proxy.token},
            headers={'X-Colab-Runtime-Proxy-Token': proxy.token}, timeout=30)
        try:
            if response.status_code == 200:
                data = response.json()
                content = data.get('content')
                if data.get('format') == 'base64':
                    import base64
                    content = base64.b64decode(content).decode('utf-8')
                marker = json.loads(content) if isinstance(content, str) else content
                if filename == 'colab-replica-owner.json':
                    if isinstance(marker, dict) and marker.get('project') == 'colab-replica':
                        return True
                elif isinstance(marker, list) and marker and all(
                    isinstance(record, dict) and isinstance(record.get('pid'), int)
                    and isinstance(record.get('starttime'), str) for record in marker
                ):
                    return True
        finally:
            response.close()
    return False


def existing_runtimes():
    """Try every configured account without exposing authentication errors or tokens."""
    for name in ACCOUNTS:
        try:
            _, session = credentials(name)
            assignments = Client(Prod(), session).list_assignments()
        except Exception as exc:
            print('Runtime listing unavailable for ' + name + ': ' + type(exc).__name__, flush=True)
            continue
        for assignment in assignments:
            try:
                if not is_owned_runtime(session, assignment):
                    print('Skipping unrelated or non-T4 runtime on ' + name, flush=True)
                    continue
            except Exception as exc:
                print('Runtime ownership unavailable on ' + name + ': ' + type(exc).__name__, flush=True)
                continue
            yield name, session, assignment


def drive_code():
    creds, _ = credentials(DRIVE_OWNER_TOKEN)
    return ('DRIVE_ACCESS_TOKEN=' + repr(creds.token) + '\nDRIVE_MODEL_NAME=' + repr(MODEL) +
            '\nDRIVE_QUOTA_PROJECT=' + repr(PROJECT) + '\n' +
            (ROOT / '_colab_drive_cache.py').read_text(encoding='utf-8'))


def main():
    for name, session, assignment in existing_runtimes():
        proxy = assignment.runtime_proxy_info
        try:
            # Refresh the owner token at the point of use, not before account discovery.
            code = drive_code()
            if '--verify-restore' in sys.argv:
                code += "\ncache_model(DRIVE_ACCESS_TOKEN, DRIVE_MODEL_NAME, '/content/drive-restore-check')\n"
                code += "\nfrom pathlib import Path\n(Path('/content/drive-restore-check') / DRIVE_MODEL_NAME).unlink()\nprint('[Drive] Restore test passed; temporary copy removed.', flush=True)\n"
            print('Using existing runtime on ' + name, flush=True)
            execute_script(session, proxy.url, proxy.token, code, timeout=3600)
        except RuntimeBusyError:
            continue
        except RemoteExecutionError:
            raise
        except Exception as exc:
            print('Runtime unavailable: ' + type(exc).__name__, flush=True)
            continue
        print('Drive cache execution finished.', flush=True)
        return
    raise RuntimeError('No idle existing Colab runtime available')


if __name__ == '__main__':
    main()
