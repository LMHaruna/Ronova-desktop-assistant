"""Compatibility check for preferences saved before global screenshot shortcuts were removed."""
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from client_store import Preferences


class PreferenceTests(unittest.TestCase):
    def test_legacy_screenshot_shortcut_is_ignored_and_dropped_on_save(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'client-preferences.json'
            path.write_text(json.dumps({'screenshot_hotkey': 'Ctrl+Alt+S', 'font_scale': 115}),
                            encoding='utf-8')
            preferences = Preferences.load(path)
            self.assertEqual(preferences.font_scale, 115)
            self.assertFalse(hasattr(preferences, 'screenshot_hotkey'))
            preferences.save(path)
            self.assertNotIn('screenshot_hotkey', json.loads(path.read_text(encoding='utf-8')))


if __name__ == '__main__':
    unittest.main()
