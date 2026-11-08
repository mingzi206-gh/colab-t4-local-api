"""Persist and restore model files using Google Drive API and short-lived access tokens.

Executed inside Colab with DRIVE_ACCESS_TOKEN and DRIVE_MODEL_NAME set by the manager.
No refresh credentials are stored in the runtime.
"""
import hashlib
from pathlib import Path
import re
import time

import requests

MODEL_NAME = 'DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic.i1-Q4_K_M.gguf'
MODEL_REVISION = 'fff50f6bb44cefedd08217dbc285fb2c41213fff'
MODEL_SHA256 = '8cf793a36e62fb078ac05c2e1bdb2121ff94736dccbede7cf81b76346e868949'
MODEL_SIZE = 5629111584
API = 'https://www.googleapis.com/drive/v3/files'
FIELDS = 'id,name,size,md5Checksum,webViewLink'
TRANSIENT = (408, 429, 500, 502, 503, 504)


def digest(path, algorithm='md5'):
    result = hashlib.new(algorithm)
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def request(session, method, url, **kwargs):
    # Retrying a create POST can create duplicate folders/files after a lost response.
    attempts = 1 if method == 'POST' else 4
    for attempt in range(attempts):
        response = None
        try:
            response = session.request(method, url, timeout=(15, 180), **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            retryable = response is None or response.status_code in TRANSIENT
            if response is not None:
                response.close()
            if not retryable or attempt == attempts - 1:
                raise error
            time.sleep(2 ** attempt)


def find(session, query):
    files = []
    token = None
    while True:
        params = {'q': query, 'fields': 'nextPageToken,files(' + FIELDS + ')', 'pageSize': 100,
                  'orderBy': 'modifiedTime desc'}
        if token:
            params['pageToken'] = token
        with request(session, 'GET', API, params=params) as response:
            result = response.json()
        files.extend(result['files'])
        token = result.get('nextPageToken')
        if not token:
            return files


def verify_remote(remote):
    if not remote.get('id') or not re.fullmatch(r'[a-fA-F0-9]{32}', remote.get('md5Checksum', '')):
        raise RuntimeError('Drive file missing required ID/MD5 metadata')
    if int(remote.get('size', 0)) <= 0:
        raise RuntimeError('Drive model is empty or has no size metadata')


def restore(session, remote, local):
    verify_remote(remote)
    if local.is_file() and local.stat().st_size == int(remote['size']) and digest(local) == remote['md5Checksum']:
        print('[Drive] Local and persistent copies match.', flush=True)
        return
    partial = local.with_suffix(local.suffix + '.drive-part')
    try:
        for attempt in range(4):
            try:
                with request(session, 'GET', API + '/' + remote['id'], params={'alt': 'media'}, stream=True) as response:
                    with partial.open('wb') as target:
                        for chunk in response.iter_content(8 * 1024 * 1024):
                            target.write(chunk)
                if partial.stat().st_size != int(remote['size']) or digest(partial) != remote['md5Checksum']:
                    raise RuntimeError('Drive download checksum mismatch')
                if local.name == MODEL_NAME and (partial.stat().st_size != MODEL_SIZE or digest(partial, 'sha256') != MODEL_SHA256):
                    raise RuntimeError('Drive download does not match pinned model SHA256/size')
                partial.replace(local)
                print('[Drive] Restored model to local disk; MD5 verified.', flush=True)
                return
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, RuntimeError):
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
    finally:
        partial.unlink(missing_ok=True)


def upload_offset(response, size):
    value = response.headers.get('Range')
    if not value:
        return 0
    match = re.fullmatch(r'bytes=0-(\d+)', value)
    if not match or not 0 <= int(match.group(1)) < size:
        raise RuntimeError('Invalid Drive resumable upload offset')
    return int(match.group(1)) + 1


def upload(session, local, model_name, folder_id):
    size = local.stat().st_size
    if not size:
        raise RuntimeError('Cannot cache an empty model')
    checksum = digest(local)
    with request(session, 'POST', 'https://www.googleapis.com/upload/drive/v3/files',
                 params={'uploadType': 'resumable', 'fields': FIELDS},
                 json={'name': model_name, 'parents': [folder_id]},
                 headers={'X-Upload-Content-Type': 'application/octet-stream', 'X-Upload-Content-Length': str(size)}) as response:
        upload_url = response.headers['Location']
    # Keep bearer tokens restricted to the official upload host, including redirects.
    from urllib.parse import urlsplit
    parsed = urlsplit(upload_url)
    if parsed.scheme != 'https' or parsed.hostname != 'www.googleapis.com' or parsed.username or chr(64) in parsed.netloc:
        raise RuntimeError('Unexpected Drive resumable upload URL')
    offset = 0
    failures = 0
    probe = False
    remote = None
    with local.open('rb') as source:
        while remote is None:
            previous = offset
            if probe:
                data = b''
                headers = {'Content-Length': '0', 'Content-Range': f'bytes */{size}'}
            else:
                source.seek(offset)
                data = source.read(16 * 1024 * 1024)
                headers = {'Content-Length': str(len(data)),
                           'Content-Range': f'bytes {offset}-{offset + len(data) - 1}/{size}'}
            response = None
            try:
                response = session.put(upload_url, data=data, headers=headers, timeout=(15, 180), allow_redirects=False)
                if response.status_code in (200, 201):
                    remote = response.json()
                elif response.status_code == 308:
                    offset = upload_offset(response, size)
                    if offset < previous or offset > size or (not probe and offset > previous + len(data)):
                        raise RuntimeError('Drive acknowledged an invalid or regressing byte offset')
                    if offset == size:
                        probe = True
                    else:
                        probe = False
                    if offset <= previous:
                        failures += 1
                    else:
                        failures = 0
                else:
                    response.raise_for_status()
                    raise RuntimeError(f'Unexpected Drive upload status: {response.status_code}')
            except requests.RequestException:
                if response is not None and response.status_code not in TRANSIENT:
                    raise
                failures += 1
                # Query the server offset after transient failures before resending a chunk.
                probe = True
            finally:
                if response is not None:
                    response.close()
            if failures >= 5:
                raise RuntimeError('Drive resumable upload exhausted retries without progress')
            if failures:
                time.sleep(2 ** failures)
            print(f'[Drive] Upload {offset}/{size} bytes', flush=True)
    verify_remote(remote)
    if int(remote['size']) != size or remote['md5Checksum'] != checksum:
        # A failed final verification must not leave a corrupt persistent cache entry.
        with request(session, 'DELETE', API + '/' + remote['id']):
            pass
        raise RuntimeError('Drive upload checksum mismatch; corrupt cache entry removed')
    return remote


def cache_locked(access_token, model_name, model_dir):
    local = Path(model_dir) / model_name
    local.parent.mkdir(parents=True, exist_ok=True)
    with requests.Session() as session:
        session.headers['Authorization'] = 'Bearer ' + access_token
        if globals().get('DRIVE_QUOTA_PROJECT'):
            session.headers['x-goog-user-project'] = DRIVE_QUOTA_PROJECT
        folders = find(session, "trashed = false and mimeType = 'application/vnd.google-apps.folder' and name = 'Colab-Model-Cache'")
        if folders:
            folder_id = folders[0]['id']
        else:
            with request(session, 'POST', API, json={'name': 'Colab-Model-Cache', 'mimeType': 'application/vnd.google-apps.folder'},
                         params={'fields': 'id'}) as response:
                folder_id = response.json()['id']
        safe_name = model_name.replace('\\', '\\\\').replace("'", "\\'")
        files = find(session, "trashed = false and '" + folder_id + "' in parents and name = '" + safe_name + "'")
        if files:
            # Duplicate names are permitted by Drive. Prefer the most recent complete entry.
            complete = []
            for item in files:
                try:
                    verify_remote(item)
                    if model_name == MODEL_NAME and int(item['size']) != MODEL_SIZE:
                        continue
                    complete.append(item)
                except (RuntimeError, ValueError, TypeError):
                    continue
            if not complete:
                raise RuntimeError('Persistent cache contains no complete model matching the pinned size; remove invalid entries and retry')
            remote = complete[0]
            restore(session, remote, local)
            if model_name == MODEL_NAME and (local.stat().st_size != MODEL_SIZE or digest(local, 'sha256') != MODEL_SHA256):
                raise RuntimeError('Restored Drive model does not match the pinned upstream SHA256/size')
        else:
            if model_name != MODEL_NAME:
                raise ValueError('This cache supports only the pinned MODEL_NAME')
            if not local.is_file() or local.stat().st_size != MODEL_SIZE or digest(local, 'sha256') != MODEL_SHA256:
                partial = local.with_suffix(local.suffix + '.part')
                try:
                    with requests.get(f'https://huggingface.co/mradermacher/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-i1-GGUF/resolve/{MODEL_REVISION}/{MODEL_NAME}', stream=True, timeout=(30, 120)) as response:
                        response.raise_for_status()
                        with partial.open('wb') as target:
                            for chunk in response.iter_content(4 * 1024 * 1024):
                                target.write(chunk)
                    if partial.stat().st_size != MODEL_SIZE or digest(partial, 'sha256') != MODEL_SHA256:
                        raise RuntimeError('Model upstream SHA256/size mismatch')
                    partial.replace(local)
                finally:
                    partial.unlink(missing_ok=True)
            remote = upload(session, local, model_name, folder_id)
            print('[Drive] Persistent upload completed; size and MD5 verified.', flush=True)
        print('[Drive] FILE_ID=' + remote['id'], flush=True)
        print('[Drive] FOLDER=https://drive.google.com/drive/folders/' + folder_id, flush=True)
        return remote


def cache_model(access_token, model_name, model_dir='/content/models'):
    if not access_token or not isinstance(model_name, str) or model_name in ('', '.', '..') or '/' in model_name or '\\' in model_name:
        raise ValueError('A token and a plain model filename are required')
    import fcntl
    # Opening as a context manager guarantees release on every exception and return path.
    with open('/content/.drive-model-cache.lock', 'a') as lock:
        print('[Drive] Waiting for model cache lock...', flush=True)
        deadline = time.monotonic() + 600
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Drive cache lock timed out')
                time.sleep(1)
        print('[Drive] Cache lock acquired.', flush=True)
        try:
            return cache_locked(access_token, model_name, model_dir)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


if __name__ == '__main__':
    cache_model(DRIVE_ACCESS_TOKEN, DRIVE_MODEL_NAME)
