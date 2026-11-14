"""Login startup, migration and UI switches without changing real startup items."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from client_autostart import RUN_KEY, VALUE_NAME, WindowsAutostart
from client_store import Preferences
import client_autostart


class LoginStartupTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.root=Path(self.folder.name)/'若娜瓦 Test'
        python=self.root/'runtime/voice-venv/Scripts/pythonw.exe'
        python.parent.mkdir(parents=True);python.touch()
        (self.root/'client_app.py').touch()
        self.startup=WindowsAutostart(self.root)

    def tearDown(self): self.folder.cleanup()

    def test_old_preferences_default_to_enabled_and_disabled_choice_survives(self):
        path=self.root/'preferences.json'
        self.assertTrue(Preferences.load(path).start_on_login)
        path.write_text('{"volume":0.4}',encoding='utf-8')
        prefs=Preferences.load(path)
        self.assertTrue(prefs.start_on_login);self.assertEqual(prefs.volume,.4)
        replace(prefs,start_on_login=False).save(path)
        self.assertFalse(Preferences.load(path).start_on_login)
        for invalid in (0,1,'false',None):
            with self.assertRaises(ValueError): replace(prefs,start_on_login=invalid).validate()

    def test_unicode_paths_with_spaces_use_windowless_python_and_absolute_script(self):
        command=self.startup.command()
        self.assertEqual(command,
            f'"{self.root / "runtime/voice-venv/Scripts/pythonw.exe"}" -EsB -X utf8 '
            f'"{self.root / "client_app.py"}" --autostart')

    def test_enable_is_idempotent_disable_only_removes_our_current_user_value(self):
        values={'OtherApplication':('unrelated.exe',client_autostart.winreg.REG_SZ)}
        key=Mock();key.__enter__=Mock(return_value=key);key.__exit__=Mock(return_value=False)
        def query(_key,name):
            if name not in values: raise FileNotFoundError(name)
            return values[name]
        def store(_key,name,_reserved,kind,value):values[name]=(value,kind)
        def delete(_key,name):
            if name not in values: raise FileNotFoundError(name)
            del values[name]
        with patch.multiple(client_autostart.winreg,
                CreateKeyEx=Mock(return_value=key),OpenKey=Mock(return_value=key),
                QueryValueEx=Mock(side_effect=query),SetValueEx=Mock(side_effect=store),
                DeleteValue=Mock(side_effect=delete)):
            self.startup.set_enabled(True);self.startup.set_enabled(True)
            self.assertEqual(values[VALUE_NAME][0],self.startup.command())
            self.assertEqual(client_autostart.winreg.SetValueEx.call_count,1)
            args=client_autostart.winreg.CreateKeyEx.call_args.args
            self.assertEqual(args[:2],(client_autostart.winreg.HKEY_CURRENT_USER,RUN_KEY))
            self.startup.set_enabled(False);self.startup.set_enabled(False)
            self.assertEqual(values,{'OtherApplication':('unrelated.exe',client_autostart.winreg.REG_SZ)})

    def test_missing_runtime_does_not_install_a_broken_startup_command(self):
        (self.root/'client_app.py').unlink()
        with patch.object(client_autostart.winreg,'CreateKeyEx') as create:
            with self.assertRaisesRegex(RuntimeError,'环境安装'):self.startup.set_enabled(True)
            create.assert_not_called()

    def test_registry_permission_error_has_actionable_message(self):
        with patch.object(client_autostart.winreg,'CreateKeyEx',side_effect=PermissionError()):
            with self.assertRaisesRegex(RuntimeError,'启动项权限'):self.startup.set_enabled(True)


class StartupSettingsTests(unittest.TestCase):
    def setUp(self):
        from test_chat_ui_stability import ChatUiTests
        ChatUiTests.setUpClass()
        self.fixture=ChatUiTests();self.fixture.setUp()
        self.controller=self.fixture.controller
        self.startup=Mock();self.controller.autostart=self.startup

    def tearDown(self): self.fixture.tearDown()

    def test_startup_sync_respects_saved_disabled_preference(self):
        self.controller.sync_autostart()
        self.startup.set_enabled.assert_called_once_with(True)
        self.controller.apply_preferences(replace(self.controller.preferences,start_on_login=False)).result(3)
        self.startup.reset_mock();self.controller.sync_autostart()
        self.startup.set_enabled.assert_called_once_with(False)

    def test_write_failure_restores_startup_and_keeps_previous_preference(self):
        with patch.object(Preferences,'save',side_effect=OSError('disk unavailable')):
            self.controller.apply_preferences(replace(self.controller.preferences,start_on_login=False)).result(3)
        self.assertTrue(self.controller.preferences.start_on_login)
        self.assertEqual(self.startup.set_enabled.call_args_list,[call(False),call(True)])

    def test_registry_failure_does_not_persist_a_false_success(self):
        self.startup.set_enabled.side_effect=RuntimeError('startup fixture failure')
        with patch.object(Preferences,'save') as save:
            self.controller.apply_preferences(replace(self.controller.preferences,start_on_login=False)).result(3)
            save.assert_not_called()
        self.assertTrue(self.controller.preferences.start_on_login)

    def test_switch_saves_and_updates_startup_without_stopping_a_call(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        pane=self.fixture.window.settings_pane
        self.fixture.window.settings();self.fixture.settle(350)
        self.assertTrue(pane.start_on_login.isChecked())
        self.controller.call.active=True;self.controller.call_requested=True
        for enabled in (False,True):
            QTest.mouseClick(pane.start_on_login_row,Qt.MouseButton.LeftButton)
            self.fixture.pending(pane)
            self.assertEqual(pane.start_on_login.isChecked(),enabled)
            self.assertEqual(Preferences.load(self.controller.preferences_path).start_on_login,enabled)
            self.startup.set_enabled.assert_called_with(enabled)
            self.assertTrue(self.controller.call.active)
            self.assertTrue(self.controller.call_requested)
        self.controller.call.active=False;self.controller.call_requested=False
        self.fixture.capture('autostart-settings.png')


if __name__=='__main__':unittest.main()
