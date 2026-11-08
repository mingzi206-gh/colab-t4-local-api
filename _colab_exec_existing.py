"""Execute a script on an existing configured runtime without releasing its VM."""
import sys
from pathlib import Path
from _colab_persist_drive import existing_runtimes, execute_script, RuntimeBusyError, RemoteExecutionError


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python _colab_exec_existing.py SCRIPT')
    source = Path(sys.argv[1]).read_text(encoding='utf-8')
    for name, session, assignment in existing_runtimes():
        proxy = assignment.runtime_proxy_info
        try:
            execute_script(session, proxy.url, proxy.token, source, filename=sys.argv[1])
            return
        except RuntimeBusyError:
            continue
        except RemoteExecutionError:
            raise
        except Exception as exc:
            print('Runtime unavailable on ' + name + ': ' + type(exc).__name__, flush=True)
    raise RuntimeError('No idle existing configured runtime')


if __name__ == '__main__':
    main()
