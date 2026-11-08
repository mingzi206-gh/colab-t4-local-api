"""Real requests through the configured gateway and optional extra targets."""
import json
import time
import requests
from settings import CONFIG, GATEWAY_HOST, GATEWAY_PORT

s = requests.Session()
s.trust_env = False
targets = [{'url': f'http://{GATEWAY_HOST}:{GATEWAY_PORT}', 'model': 'deepseek-heretic-9b'}]
targets += CONFIG['verification_targets']
for target in targets:
    base_url = target['url'].rstrip('/')
    if not base_url.endswith('/v1'):
        base_url += '/v1'
    model = target['model']
    r = s.get(base_url + '/models', timeout=30)
    print('MODELS', r.status_code, r.text[:1200], flush=True)
    r.raise_for_status()
    start = time.monotonic()
    r = s.post(base_url + '/chat/completions', json={
        'model': model,
        'messages': [{'role': 'user', 'content': 'Reply with exactly OK.'}],
        'temperature': 0,
        'stream': False,
    }, timeout=300)
    print('CHAT', base_url, r.status_code, 'seconds', round(time.monotonic()-start, 2), r.text[:8000], flush=True)
    r.raise_for_status()
    data = r.json()
    choice = data['choices'][0]
    if not choice['message'].get('content') or choice.get('finish_reason') != 'stop':
        raise RuntimeError(f'Incomplete chat response on {base_url}')
