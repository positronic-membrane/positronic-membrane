import unittest

class Watcher:
    def __init__(self):
        self.is_active = False

    def start(self):
        self.is_active = True

    def stop(self):
        self.is_active = False


class Orchestrator:
    def __init__(self, watcher):
        self.watcher = watcher
        self.status = "idle"

    def execute(self):
        self.watcher.start()
        self.status = "running"
        return self.status


class TestWatcherAndOrchestrator(unittest.TestCase):
    def test_orchestrator_uses_watcher(self):
        """Test that the orchestrator correctly instantiates and invokes the watcher."""
        watcher = Watcher()
        orchestrator = Orchestrator(watcher)
        
        self.assertFalse(watcher.is_active)
        self.assertEqual(orchestrator.status, "idle")
        
        result = orchestrator.execute()
        
        self.assertEqual(result, "running")
        self.assertTrue(watcher.is_active)
        self.assertEqual(orchestrator.status, "running")


if __name__ == '__main__':
    unittest.main()