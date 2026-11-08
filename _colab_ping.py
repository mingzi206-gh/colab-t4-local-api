"""Fast keep-alive and health probe for Colab T4 session."""
import json
from pathlib import Path
import urllib.request


def main():
    status = 0
    try:
        with urllib.request.urlopen('http://127.0.0.1:11434/health', timeout=2) as response:
            if response.status == 200 and json.loads(response.read(65536)).get('status') == 'ok':
                status = 200
    except Exception:
        pass
    path = Path('/content/cf_url.txt')
    cf_url = path.read_text().strip() if path.exists() else 'NONE'
    print(f'ALIVE status={status} url={cf_url}', flush=True)


if __name__ == '__main__':
    main()
