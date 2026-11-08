"""Stop this replica's remote processes without allocating or releasing Colab VMs."""
from _colab_persist_drive import existing_runtimes, execute_script, RuntimeBusyError, RemoteExecutionError
from settings import ONE_SHOT_SCRIPT


def main():
    source = ONE_SHOT_SCRIPT.read_text(encoding='utf-8')
    # Preserve notebook arguments even if process cleanup fails. No API key is required.
    source = ("import sys\n__colab_previous_argv = sys.argv\n"
              "try:\n    sys.argv = ['remote', '--stop']\n" +
              '\n'.join('    ' + line for line in source.splitlines()) +
              '\nfinally:\n    sys.argv = __colab_previous_argv\n')
    stopped = 0
    busy = 0
    for name, session, assignment in existing_runtimes():
        proxy = assignment.runtime_proxy_info
        try:
            execute_script(session, proxy.url, proxy.token, source, timeout=120,
                           filename='_colab_stop_existing.py')
        except RuntimeBusyError:
            busy += 1
            print('Runtime busy on ' + name + '; processes were not interrupted', flush=True)
            continue
        except RemoteExecutionError:
            raise
        print('Stopped replica processes on ' + name + '; VM and cache retained', flush=True)
        stopped += 1
    if not stopped:
        raise RuntimeError('No idle project runtime available' if busy else 'No project runtime found')
    if busy:
        raise RuntimeError('Some project runtimes were busy; retry after their execution finishes')


if __name__ == '__main__':
    main()
