"""Mac notification text; no notification is shown."""
import unittest
from unittest.mock import patch

from collector import collector


class NotificationTitleTests(unittest.TestCase):
    def shown_title(self, incident_state):
        calls = []
        with patch.object(collector.shutil, "which", lambda name: "/bin/terminal-notifier" if name == "terminal-notifier" else None), \
             patch.object(collector.subprocess, "run", lambda command, **kwargs: calls.append(command)):
            collector.show_notification({"incident_id": "i-1", "incident_state": incident_state,
                                         "payload": {"reason": "No meaningful progress within the task threshold"}})
        return calls[0][calls[0].index("-title") + 1]

    def test_first_notice_says_the_task_stalled(self):
        self.assertEqual(self.shown_title("open"), "Lighthouse: task stalled")

    def test_escalation_says_escalated(self):
        self.assertEqual(self.shown_title("escalated"), "Lighthouse: incident escalated")


if __name__ == "__main__":
    unittest.main()
