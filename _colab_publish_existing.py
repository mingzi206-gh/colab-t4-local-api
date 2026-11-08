"""Publish an existing healthy Colab tunnel to the local gateway state file."""
import base64
import re
from _colab_persist_drive import existing_runtimes


def main():
    from _colab_local_gateway import check_tunnel_health, set_active_url
    for name, session, assignment in existing_runtimes():
        proxy = assignment.runtime_proxy_info
        try:
            response = session.get(proxy.url.rstrip('/') + '/api/contents/content/cf_url.txt',
                params={'colab-runtime-proxy-token': proxy.token},
                headers={'X-Colab-Runtime-Proxy-Token': proxy.token}, timeout=30)
            response.raise_for_status()
            data = response.json()
            url = data['content']
            if data.get('format') == 'base64':
                url = base64.b64decode(url, validate=True).decode()
            url = url.strip()
            if not re.fullmatch(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', url):
                raise RuntimeError('Invalid public tunnel URL')
            if not check_tunnel_health(url):
                raise RuntimeError('Public tunnel is not healthy')
        except Exception as exc:
            print('Runtime unavailable on ' + name + ': ' + type(exc).__name__, flush=True)
            continue
        set_active_url(url)
        print('Published', url, flush=True)
        return
    raise RuntimeError('No healthy existing Colab tunnel available')


if __name__ == '__main__':
    main()
