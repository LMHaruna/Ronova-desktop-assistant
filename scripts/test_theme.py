"""Theme persistence and live switching without external services."""
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPixmap, QTextCursor
from PySide6.QtTest import QTest

import test_model_providers as fixtures
from client_store import Preferences
from client_theme import COLORS, LIGHT_COLORS
from client_workspace_history import MessageText
from client_codex import CodexAppServer
from client_workspace import WorkspacePane, WorkspaceTurn


class ThemePreferences(unittest.TestCase):
    def test_defaults_validation_and_round_trip(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'preferences.json'
            self.assertEqual(Preferences.load(path).theme, 'light')
            Preferences(theme='dark').save(path)
            self.assertEqual(Preferences.load(path).theme, 'dark')
            with self.assertRaises(ValueError):
                Preferences(theme='other').validate()


class ThemeUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.ProviderUI.setUpClass.__func__(cls)

    setUp = fixtures.ProviderUI.setUp
    tearDown = fixtures.ProviderUI.tearDown

    def wait_for_theme(self, name):
        deadline = time.monotonic() + 5
        while self.window._theme != name and time.monotonic() < deadline:
            self.window.settings_pane.refresh()
            QTest.qWait(20)
        self.assertEqual(self.window._theme, name)

    def test_switch_persists_and_recolors_existing_pages(self):
        switch = self.window.settings_pane.theme_switch
        switch.click()
        self.wait_for_theme('dark')
        self.assertEqual(self.controller.preferences.theme, 'dark')
        self.assertEqual(Preferences.load(self.controller.preferences_path).theme, 'dark')
        self.assertEqual(COLORS['surface'], '#211F1F')
        self.assertEqual(COLORS['text'], '#F6F2F3')
        self.assertIn('#211F1F', self.window.overlay.surface.styleSheet())
        self.assertIn('QWidget#chatPage QWidget#composer {background:#292627;}', self.window.overlay.surface.styleSheet())
        switch.click()
        self.wait_for_theme('light')
        self.assertEqual(COLORS, LIGHT_COLORS)
        self.assertIn(LIGHT_COLORS['surface'], self.window.overlay.surface.styleSheet())
        self.assertNotIn('QWidget#chatPage QWidget#composer {background:#292627;}', self.window.overlay.surface.styleSheet())

    def test_switch_accepts_horizontal_drag(self):
        switch = self.window.settings_pane.theme_switch
        QTest.mousePress(switch, Qt.MouseButton.LeftButton, pos=QPoint(4, 12))
        QTest.mouseRelease(switch, Qt.MouseButton.LeftButton, pos=QPoint(38, 12))
        self.wait_for_theme('dark')
        QTest.mousePress(switch, Qt.MouseButton.LeftButton, pos=QPoint(38, 12))
        QTest.mouseRelease(switch, Qt.MouseButton.LeftButton, pos=QPoint(4, 12))
        self.wait_for_theme('light')

    def test_existing_markdown_file_links_follow_theme(self):
        message = MessageText()
        message.show_text('修改了 [client_workspace.py](https://example.com/file) 和 `settings.py`。', markdown=True)
        def link_format():
            cursor = QTextCursor(message.document())
            cursor.setPosition(message.toPlainText().index('client_workspace.py'))
            cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
            return cursor.charFormat()

        try:
            self.assertEqual(link_format().foreground().color().name(), '#2453a6')
            self.window.settings_pane.theme_switch.click()
            self.wait_for_theme('dark')
            self.assertEqual(link_format().foreground().color().name(), '#a9c7ff')
            self.assertEqual(link_format().anchorHref(), 'https://example.com/file')
            self.window.settings_pane.theme_switch.click()
            self.wait_for_theme('light')
            self.assertEqual(link_format().foreground().color().name(), '#2453a6')
        finally:
            message.deleteLater()

    def test_workspace_lists_use_readable_text_in_both_themes(self):
        self.window.apply_theme('dark')
        server = CodexAppServer(self.controller.preferences_path.parent / 'theme-test-codex')
        pane = WorkspacePane(QPixmap(), '', lambda: None, server, lambda: None)
        try:
            turn = WorkspaceTurn('请检查工作区', state='done')
            pane.active_chat.turns.append(turn)
            pane.render_history()
            pane.show()
            self.app.processEvents()
            bubble = pane.history.rows[turn].bubble
            self.assertIn('background:#5A1D24', bubble.styleSheet())
            self.assertEqual(pane.history.rows[turn].question.palette().text().color().name(), '#f6f2f3')
            self.assertEqual(pane.tree.palette().text().color().name(), '#f6f2f3')
            self.assertIn('#292627', pane.sidebar.styleSheet())
            self.assertIn('#292627', pane.composer.styleSheet())
            self.assertIn('QProgressBar#workQuotaBar::chunk { background:#8B1015;', pane.sidebar.styleSheet())
            self.window.apply_theme('light')
            self.app.processEvents()
            self.assertEqual(pane.tree.palette().text().color().name(), LIGHT_COLORS['text'].lower())
            self.assertIn(LIGHT_COLORS['background'], pane.sidebar.styleSheet())
            self.assertEqual(pane.composer.styleSheet(), '')
            self.assertEqual(bubble.styleSheet(), '')
            self.window.apply_theme('dark')
            self.app.processEvents()
            self.assertEqual(pane.tree.palette().text().color().name(), '#f6f2f3')
            self.assertIn('#292627', pane.sidebar.styleSheet())
            self.assertIn('#292627', pane.composer.styleSheet())
            self.assertIn('background:#5A1D24', bubble.styleSheet())
        finally:
            pane.close()
            server.close()


if __name__ == '__main__':
    unittest.main()
