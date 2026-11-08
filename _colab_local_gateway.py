"""Pure-Python local OpenAI-compatible gateway and configurable Colab account manager."""
import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from colab_cli.client import Accelerator, Client, Prod, Variant
from _colab_persist_drive import (credentials as drive_credentials, drive_code,
    execute_script, script_code, idle_kernel, is_owned_runtime, RuntimeBusyError, RemoteExecutionError)
import settings
from settings import (STATE_FILE, ONE_SHOT_SCRIPT, PING_SCRIPT, ACCOUNTS,
                      GATEWAY_HOST, GATEWAY_PORT, LOG_FILE)

current_account_idx = 0
active_runtime_url = ""
active_runtime_token = ""
active_kernel_id = None  # Compatibility only: IDs are discovered on the target VM.
last_deploy_time = 0.0
_deployment_lock = threading.Lock()
_stop_event = threading.Event()


def get_active_url() -> str:
    try:
        return STATE_FILE.read_text(encoding="utf-8").strip().rstrip("/")
    except FileNotFoundError:
        return ""


def set_active_url(url: str):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_name(STATE_FILE.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(url.strip().rstrip("/"), encoding="utf-8")
        os.replace(temporary, STATE_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def check_tunnel_health(url: str) -> bool:
    if not re.fullmatch(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", url):
        return False
    try:
        request = urllib.request.Request(url + "/health", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status != 200 or json.loads(response.read(65536)).get("status") != "ok":
                return False
        # A healthy service with an obsolete API key is not usable by this installation.
        request = urllib.request.Request(url + "/v1/models", headers={
            "Authorization": "Bearer " + settings.get_api_key(), "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status == 200 and bool(json.loads(response.read(1048576)).get("data"))
    except Exception:
        return False


def get_client_for_account(token_name: str) -> Client:
    _, session = drive_credentials(token_name)
    return Client(Prod(), session)


def run_code_on_vm(url: str, token: str, code: str, timeout: float = 300.0, session=None) -> str:
    if session is None:
        session = get_client_for_account(ACCOUNTS[current_account_idx]).session
    return execute_script(session, url, token, code, timeout=timeout)


def deployment_source():
    source = ONE_SHOT_SCRIPT.read_text(encoding="utf-8")
    # Restore the persistent owner-account cache before starting the inference server.
    try:
        cache_source = script_code(drive_code(), "_colab_drive_cache.py")
        # Cache errors should not prevent an otherwise usable inference deployment.
        source = ("try:\n" + "\n".join("    " + line for line in cache_source.splitlines()) +
                  "\nexcept Exception as exc:\n    print('[Drive] Cache unavailable:', type(exc).__name__, flush=True)\n" + source)
    except Exception as exc:
        print("[Gateway] Drive cache unavailable: " + type(exc).__name__, flush=True)
    return ("REMOTE_API_KEY=" + repr(settings.get_api_key()) + "\nREMOTE_CONTEXT_SIZE=" +
            repr(settings.CONFIG['context_size']) + "\n" + source)


def deploy_on_next_available_account() -> str:
    global current_account_idx, active_runtime_url, active_runtime_token, last_deploy_time
    if not _deployment_lock.acquire(blocking=False):
        return ""
    try:
        for offset in range(len(ACCOUNTS)):
            idx = (current_account_idx + offset) % len(ACCOUNTS)
            name = ACCOUNTS[idx]
            try:
                client = get_client_for_account(name)
                assignments = client.list_assignments()
            except Exception as exc:
                print(f"[Gateway] Account {name} unavailable: {type(exc).__name__}", flush=True)
                continue
            # A setup or transport error does not prove that the allocated VM is lost.
            # Never release an assignment merely because setup or its websocket timed out.
            fresh = not assignments
            if fresh:
                try:
                    assignments = [client.assign(uuid.uuid4(), variant=Variant.GPU, accelerator=Accelerator.T4)]
                except Exception as exc:
                    print(f"[Gateway] Allocation failed on {name}: {type(exc).__name__}", flush=True)
                    continue
            for assignment in assignments:
                if not fresh and getattr(assignment, 'accelerator', None) != Accelerator.T4:
                    continue
                proxy = assignment.runtime_proxy_info
                try:
                    if fresh:
                        # Durable ownership survives failed setup before any process was launched.
                        marker = client.session.put(proxy.url.rstrip('/') + '/api/contents/content/colab-replica-owner.json',
                            params={'colab-runtime-proxy-token': proxy.token},
                            headers={'X-Colab-Runtime-Proxy-Token': proxy.token},
                            json={'type': 'file', 'format': 'text', 'content': '{"project":"colab-replica"}'}, timeout=30)
                        marker.raise_for_status()
                    elif not is_owned_runtime(client.session, assignment):
                        print(f'[Gateway] Skipping unrelated or non-T4 runtime on {name}', flush=True)
                        continue
                    # Refresh the owner token just before use, after checking kernel availability.
                    idle_kernel(client.session, proxy.url, proxy.token)
                    out = run_code_on_vm(proxy.url, proxy.token, deployment_source(),
                                         timeout=3600, session=client.session)
                    matches = re.findall(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", out)
                    new_url = next((url for url in reversed(matches) if check_tunnel_health(url)), "")
                    if not new_url:
                        raise RuntimeError("No healthy authenticated tunnel in setup output")
                    set_active_url(new_url)
                    current_account_idx = idx
                    active_runtime_url, active_runtime_token = proxy.url, proxy.token
                    last_deploy_time = time.monotonic()
                    print(f"[Gateway] READY -> {new_url} ({name})", flush=True)
                    return new_url
                except RemoteExecutionError as exc:
                    print(f"[Gateway] {exc}; preserving VM until next recovery cycle", flush=True)
                    # Timeout can leave setup running. Do not launch another deployment now.
                    return ""
                except Exception as exc:
                    print(f"[Gateway] Runtime on {name} unavailable: {type(exc).__name__}", flush=True)
        return ""
    finally:
        _deployment_lock.release()


def watchdog_loop(stop_event=None):
    stop_event = stop_event or _stop_event
    while not stop_event.is_set():
        healthy = False
        try:
            healthy = check_tunnel_health(get_active_url())
            if healthy:
                # Public authenticated health is sufficient. Do not execute artificial keep-alive cells.
                pass
            else:
                print('[Gateway] Tunnel unavailable; recovering', flush=True)
                deploy_on_next_available_account()
        except Exception as exc:
            print('[Gateway] Watchdog warning: ' + type(exc).__name__, flush=True)
        stop_event.wait(90 if healthy else 15)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    max_body_bytes = 8 * 1024 * 1024

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        pass

    def _proxy(self):
        path = self.path.split('?', 1)[0]
        allowed = {'GET': {'/health', '/v1/models'}, 'POST': {'/v1/chat/completions'}}
        if path not in allowed.get(self.command, set()) or '?' in self.path:
            self.send_Error_Json(404, 'Endpoint unavailable')
            return
        if self.headers.get('Transfer-Encoding'):
            self.send_Error_Json(400, 'Transfer-Encoding is unsupported')
            return
        lengths = self.headers.get_all('Content-Length') if hasattr(self.headers, 'get_all') else [self.headers.get('Content-Length', '0')]
        if not lengths:
            lengths = ['0']
        if len(lengths) != 1 or len(lengths[0]) > 10 or not re.fullmatch(r'[0-9]+', lengths[0]):
            self.send_Error_Json(400, 'Invalid Content-Length')
            return
        content_len = int(lengths[0])
        if content_len > self.max_body_bytes:
            self.send_Error_Json(413, 'Request body too large')
            return
        try:
            body = self.rfile.read(content_len) if content_len else None
        except (OSError, TimeoutError):
            self.send_Error_Json(408, 'Request body timeout')
            return
        if content_len and len(body) != content_len:
            self.send_Error_Json(400, 'Incomplete request body')
            return
        base_url = get_active_url()
        if not base_url:
            self.send_Error_Json(503, 'Colab tunnel is initializing')
            return
        if body and self.command == 'POST':
            try:
                payload = json.loads(body)
                if isinstance(payload, dict) and payload.get('max_tokens') is None and payload.get('max_completion_tokens') is None:
                    payload['max_tokens'] = 4096
                    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            except (ValueError, UnicodeDecodeError):
                pass  # Let the upstream API report malformed request bodies.
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': self.headers.get('Accept', '*/*'),
                   'Accept-Encoding': 'identity'}
        if 'Content-Type' in self.headers:
            headers['Content-Type'] = self.headers['Content-Type']
        if path != '/health':
            headers['Authorization'] = 'Bearer ' + settings.get_api_key()
        # POST may already have generated output when a connection fails. Never replay it.
        attempts = 1 if self.command == 'POST' else 2
        for attempt in range(attempts):
            started = False
            try:
                request = urllib.request.Request(base_url + self.path, data=body, headers=headers, method=self.command)
                with urllib.request.urlopen(request, timeout=300) as response:
                    self.send_response(response.status)
                    started = True
                    for key, value in response.headers.items():
                        if key.lower() not in {'connection', 'transfer-encoding', 'keep-alive', 'proxy-authenticate',
                                               'proxy-authorization', 'te', 'trailer', 'upgrade'}:
                            self.send_header(key, value)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.close_connection = True
                    reader = getattr(response, 'read1', response.read)
                    while True:
                        chunk = reader(4096)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    return
            except urllib.error.HTTPError as exc:
                if not started and exc.code in (502, 503, 530) and attempt + 1 < attempts:
                    exc.close()
                    continue
                if started:
                    self.close_connection = True
                    return
                try:
                    error_body = exc.read(1048576)
                finally:
                    exc.close()
                self.send_response(exc.code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(error_body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                self.wfile.write(error_body)
                return
            except Exception as exc:
                if started:
                    self.close_connection = True
                    return
                if attempt + 1 < attempts:
                    continue
                self.send_Error_Json(502, 'Upstream unavailable: ' + type(exc).__name__)
                return

    def send_Error_Json(self, code: int, msg: str):
        payload = json.dumps({'error': {'message': msg, 'type': 'gateway_error'}}).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(payload)

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Content-Length', '0')
        self.end_headers()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class Tee:
    def __init__(self, console, log):
        self.console, self.log = console, log

    def write(self, text):
        self.console.write(text)
        self.log.write(text)
        return len(text)

    def flush(self):
        self.console.flush()
        self.log.flush()


def main():
    # Bind first: a duplicate gateway must not allocate another VM.
    server = ThreadedHTTPServer((GATEWAY_HOST, GATEWAY_PORT), ProxyHandler)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    with open(LOG_FILE, 'a', encoding='utf-8', buffering=1) as log_fp:
        sys.stdout, sys.stderr = Tee(sys.stdout, log_fp), Tee(sys.stderr, log_fp)
        _stop_event.clear()
        thread = threading.Thread(target=watchdog_loop, args=(_stop_event,), daemon=True)
        thread.start()
        try:
            print(f'[Gateway] Started (PID={os.getpid()}) on http://{GATEWAY_HOST}:{GATEWAY_PORT}/v1', flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            _stop_event.set()
            server.server_close()
            sys.stdout, sys.stderr = original_stdout, original_stderr
            # Do not close a log still in use by an in-flight remote execution.
            if thread.is_alive():
                thread.join(timeout=1)


if __name__ == '__main__':
    main()
