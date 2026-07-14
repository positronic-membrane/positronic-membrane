import os
import threading
import time


class DirectoryWatcher:
    def __init__(self, path, callback=None):
        self.path = path
        self.callback = callback

    def _get_state(self):
        ignored_items = {
            ".git",
            ".venv",
            "venv",
            "janus.db",
            "janus.db-journal",
            "janus.db-wal",
            "janus.db-shm",
            ".DS_Store",
            "__pycache__",
            ".janus_snapshots",
            "data",
            ".janus_sandboxes"
        }
        state = {}
        for root, dirs, files in os.walk(self.path):
            # Prune ignored directories in-place to avoid traversing them
            dirs[:] = [d for d in dirs if d not in ignored_items]
            for file in files:
                if (file in ignored_items or
                    file.endswith((".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3"))):
                    continue
                filepath = os.path.join(root, file)
                try:
                    state[filepath] = os.path.getmtime(filepath)
                except OSError:
                    pass
        return state

    def watch(self, duration=None, interval=1.0, stop_event=None):
        if stop_event is None:
            stop_event = threading.Event()

        end_time = time.time() + duration if duration is not None else None
        previous_state = self._get_state()
        final_changes = None

        while not stop_event.is_set():
            if end_time is not None and time.time() >= end_time:
                break

            current_state = self._get_state()

            if current_state != previous_state:
                added = set(current_state.keys()) - set(previous_state.keys())
                removed = set(previous_state.keys()) - set(current_state.keys())
                modified = {
                    k for k in set(current_state.keys()) & set(previous_state.keys())
                    if current_state[k] != previous_state[k]
                }

                changes = {
                    'added': list(added),
                    'removed': list(removed),
                    'modified': list(modified)
                }

                if self.callback:
                    self.callback(changes)

                final_changes = changes
                previous_state = current_state

            stop_event.wait(interval)

        return final_changes
