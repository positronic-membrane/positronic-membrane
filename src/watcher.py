import os
import time
import threading

class DirectoryWatcher:
    def __init__(self, path, callback=None):
        self.path = path
        self.callback = callback

    def _get_state(self):
        state = {}
        for root, _, files in os.walk(self.path):
            for file in files:
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