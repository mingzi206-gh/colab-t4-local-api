"""Verified official llama.cpp CUDA build and authenticated Cloudflare tunnel for Colab."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

import requests

MODEL_NAME = 'DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.i1-Q4_K_M.gguf'
MODEL_REPO = 'mradermacher/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-i1-GGUF'
MODEL_REVISION = 'fff50f6bb44cefedd08217dbc285fb2c41213fff'
MODEL_SHA256 = '8cf793a36e62fb078ac05c2e1bdb2121ff94736dccbede7cf81b76346e868949'
MODEL_SIZE = 5629111584
RELEASE = 'b12202'
ROOT = Path('/content')
STATE = ROOT / 'colab-runtime.json'


def download(url, target, checksum, size=None):
    if not re.fullmatch(r'[0-9a-fA-F]{64}', checksum or ''):
        raise RuntimeError('Official asset has no valid SHA256 digest')
    digest = hashlib.sha256()
    count = 0
    try:
        with requests.get(url, stream=True, timeout=(30, 120)) as response:
            response.raise_for_status()
            with Path(target).open('wb') as output:
                for chunk in response.iter_content(4 * 1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    count += len(chunk)
        if digest.hexdigest() != checksum.lower() or (size is not None and count != int(size)):
            raise RuntimeError('Official asset size/SHA256 mismatch')
    except BaseException:
        Path(target).unlink(missing_ok=True)
        raise


def ensure_model(model_dir):
    model = Path(model_dir) / MODEL_NAME
    model.parent.mkdir(parents=True, exist_ok=True)
    if model.is_file() and model.stat().st_size == MODEL_SIZE:
        result = hashlib.sha256()
        with model.open('rb') as source:
            for chunk in iter(lambda: source.read(8 * 1024 * 1024), b''):
                result.update(chunk)
        if result.hexdigest() == MODEL_SHA256:
            return str(model)
    partial = model.with_suffix(model.suffix + '.part')
    try:
        download(f'https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{MODEL_NAME}',
                 partial, MODEL_SHA256, MODEL_SIZE)
        partial.replace(model)
    finally:
        partial.unlink(missing_ok=True)
    return str(model)


def extract_archive(archive, destination):
    # The data filter rejects escaping paths, unsafe links and device nodes.
    if not hasattr(tarfile, 'data_filter'):
        raise RuntimeError('Python with tarfile.data_filter is required (Python 3.12+ recommended)')
    with tarfile.open(archive) as package:
        package.extractall(destination, filter='data')


def install_tools():
    subprocess.run(['apt-get', 'update', '-qq'], check=True)
    subprocess.run(['apt-get', 'install', '-y', '-qq', 'zstd', 'pciutils'], check=True)
    backend_dir = Path('/usr/local/lib/llama-cpp')
    with tempfile.TemporaryDirectory(prefix='colab-official-') as temporary:
        staging = Path(temporary)
        base = 'https://pkg.cloudflare.com/cloudflared/'
        response = requests.get(base + 'dists/any/main/binary-amd64/Packages', timeout=60)
        response.raise_for_status()
        packages = [dict(line.split(': ', 1) for line in block.splitlines() if ': ' in line)
                    for block in response.text.split('\n\n')]
        package = next(p for p in packages if p.get('Package') == 'cloudflared' and p.get('Architecture') == 'amd64')
        # Never follow a metadata-supplied URL to a different host.
        filename = package['Filename']
        if filename.startswith(('/', 'http:','https:')) or '..' in Path(filename).parts:
            raise RuntimeError('Unsafe Cloudflare package filename')
        download(base + filename, staging / 'cloudflared.deb', package['SHA256'], package.get('Size'))
        subprocess.run(['dpkg-deb', '-x', str(staging / 'cloudflared.deb'), str(staging / 'cloudflare')], check=True)
        shutil.copy2(staging / 'cloudflare/usr/bin/cloudflared', '/usr/local/bin/cloudflared')
        os.chmod('/usr/local/bin/cloudflared', 0o755)
        response = requests.get(f'https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{RELEASE}', timeout=60)
        response.raise_for_status()
        assets = {asset['name']: asset for asset in response.json()['assets']}
        extracted = staging / 'backend'
        extracted.mkdir()
        for name in [f'llama-{RELEASE}-bin-ubuntu-cuda-12.8-x64.tar.gz',
                     f'cudart-llama-{RELEASE}-bin-ubuntu-cuda-12.8-x64.tar.gz']:
            asset = assets[name]
            url = asset['browser_download_url']
            if not url.startswith('https://github.com/ggml-org/llama.cpp/releases/download/'):
                raise RuntimeError('Unexpected backend release URL')
            download(url, staging / name, (asset.get('digest') or '').removeprefix('sha256:'), asset['size'])
            extract_archive(staging / name, extracted)
        binaries = list(extracted.rglob('llama-server'))
        if len(binaries) != 1:
            raise RuntimeError('Expected exactly one llama-server in official release')
        relative = binaries[0].relative_to(extracted)
        # No cached partially extracted installation is ever treated as verified.
        if backend_dir.exists():
            shutil.rmtree(backend_dir)
        shutil.copytree(extracted, backend_dir, symlinks=True)
        return backend_dir, str(backend_dir / relative)


def process_identity(pid):
    # /proc starttime disambiguates a recycled PID from our original process.
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def stop_process(record):
    pid = record['pid']
    if not isinstance(pid, int) or pid <= 1 or not record.get('starttime'):
        raise RuntimeError('Invalid managed process identity')
    if process_identity(pid) != record.get('starttime'):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while process_identity(pid) == record['starttime'] and time.monotonic() < deadline:
            time.sleep(0.1)
        if process_identity(pid) == record['starttime']:
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_runtime():
    if STATE.exists():
        records = json.loads(STATE.read_text())
        for record in reversed(records):
            stop_process(record)
        STATE.unlink(missing_ok=True)
    (ROOT / 'cf_url.txt').unlink(missing_ok=True)


def launch(command, logfile, records, env=None):
    with open(logfile, 'w') as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
    record = {'pid': process.pid, 'starttime': process_identity(process.pid)}
    if record['starttime'] is None:
        process.wait(timeout=5)
        raise RuntimeError(f'Process exited during startup; see {logfile}')
    records.append(record)
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(records))
    temporary.replace(STATE)
    return process


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    if '--stop' in sys.argv:
        stop_runtime()
        return
    key = globals().get('REMOTE_API_KEY') or os.environ.get('REMOTE_API_KEY', '')
    if len(key) < 32 or any(c.isspace() for c in key):
        raise RuntimeError('Set REMOTE_API_KEY to a random secret of at least 32 non-whitespace characters')
    context = int(globals().get('REMOTE_CONTEXT_SIZE') or os.environ.get('REMOTE_CONTEXT_SIZE', '131072'))
    if not 512 <= context <= 131072:
        raise ValueError('REMOTE_CONTEXT_SIZE must be between 512 and 131072; large contexts may exceed GPU memory')
    # Fail before downloads when the runtime has no CUDA device.
    subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader'], check=True)
    stop_runtime()
    backend_dir, binary = install_tools()
    model_dir = ROOT / 'models'
    model_dir.mkdir(exist_ok=True)
    model = ensure_model(model_dir)
    keyfile = ROOT / '.inference-api-key'
    descriptor = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(keyfile, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        output.write(key)
    env = os.environ.copy()
    env.pop('REMOTE_API_KEY', None)
    library_dirs = sorted({str(p.parent) for p in backend_dir.rglob('*.so*')})
    env['LD_LIBRARY_PATH'] = ':'.join(library_dirs + ['/usr/lib64-nvidia', '/usr/local/nvidia/lib64', '/usr/local/cuda/lib64', env.get('LD_LIBRARY_PATH', '')])
    env['GGML_BACKEND_DIR'] = str(Path(binary).parent)
    env['CUDA_VISIBLE_DEVICES'] = '0'
    records = []
    try:
        server = launch([binary, '--model', model, '--alias', 'deepseek-heretic-9b',
                         '--host', '127.0.0.1', '--port', '11434', '--api-key-file', str(keyfile),
                         '-ngl', '99', '-c', str(context), '-np', '1', '-t', '2', '-tb', '2',
                         '-b', '512', '-ub', '128'], '/tmp/llama-server.log', records, env)
        headers = {'Authorization': 'Bearer ' + key}
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError('llama-server exited; see /tmp/llama-server.log')
            try:
                with requests.get('http://127.0.0.1:11434/health', headers=headers, timeout=2) as response:
                    if response.status_code == 200:
                        break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('Inference server startup timeout; see /tmp/llama-server.log')
        # Exercise the generation path before publishing the tunnel, including CUDA first-use work.
        with requests.post('http://127.0.0.1:11434/v1/chat/completions', headers=headers,
                           json={'model': 'deepseek-heretic-9b', 'messages': [{'role': 'user', 'content': 'Hi'}],
                                 'max_tokens': 1, 'stream': False}, timeout=180) as response:
            response.raise_for_status()
            if not response.json().get('choices'):
                raise RuntimeError('Warmup returned no completion')
        with requests.get('http://127.0.0.1:11434/v1/models', timeout=5) as response:
            if response.status_code not in (401, 403):
                raise RuntimeError('API authentication check failed; refusing public tunnel')
        tunnel = launch(['/usr/local/bin/cloudflared', 'tunnel', '--url', 'http://127.0.0.1:11434',
                         '--no-autoupdate'], '/tmp/cloudflared.log', records, env)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if tunnel.poll() is not None or server.poll() is not None:
                raise RuntimeError('Server or tunnel exited; see /tmp/cloudflared.log and /tmp/llama-server.log')
            match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', Path('/tmp/cloudflared.log').read_text())
            if match:
                public_url = match.group(0)
                try:
                    with requests.get(public_url + '/v1/models', headers=headers, timeout=10) as response:
                        if response.status_code == 200:
                            (ROOT / 'cf_url.txt').write_text(public_url)
                            print('OPENAI COMPATIBLE URL: ' + public_url + '/v1', flush=True)
                            print('MODEL NAME: deepseek-heretic-9b', flush=True)
                            return
                except requests.RequestException:
                    pass
            time.sleep(1)
        raise RuntimeError('Public tunnel readiness timeout; see /tmp/cloudflared.log')
    except BaseException:
        for record in reversed(records):
            stop_process(record)
        STATE.unlink(missing_ok=True)
        (ROOT / 'cf_url.txt').unlink(missing_ok=True)
        raise
    finally:
        keyfile.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
