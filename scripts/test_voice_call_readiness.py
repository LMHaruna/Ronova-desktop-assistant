"""Voice preload admission checks; fixtures never load models or open devices."""
from dataclasses import replace
from pathlib import Path
import io
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from client_controller import DesktopController
from client_fixtures import FakeCall, FakeChat, FakeVoice, fake_router
from client_store import Preferences
from local_voice import LocalVoice

ROOT = Path(__file__).resolve().parents[1]


class LoadingVoice(FakeVoice):
    engine = 'gpt'

    def __init__(self):
        super().__init__()
        self.ready = threading.Event()
        self.prepare_error = None
        self.failure = False
        self.prepare_calls = self.releases = 0

    @property
    def preparing(self):
        return self.prepare_calls > 0 and not self.ready.is_set() and not self.prepare_error and not self.failure

    def prepare(self):
        self.prepare_calls += 1
        if self.failure: raise RuntimeError('fixture preparation failure')
        return self.ready

    def release_worker(self):
        self.releases += 1
        self.ready.clear()
        self.prepare_error = None


class ReadinessBase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.voice = LoadingVoice()
        self.router, self.cloud_chat, self.cloud_voice = fake_router()
        self.chat = FakeChat(); self.chat.outputs_audio = False
        self.router.factories['deepseek'] = lambda: (self.chat, self.voice)
        Preferences(engine='deepseek', autoplay=False).save(Path(folder.name)/'config/client-preferences.json')
        self.ringtone = Mock(); self.ringtone.stop.return_value = True
        self.controller = DesktopController(folder.name, router=self.router, call_factory=FakeCall,
            ringtone_factory=lambda *args, **kwargs: self.ringtone)
        self.addCleanup(lambda: self.controller.close().result(5))

    def assert_blocked(self):
        self.assertFalse(self.controller.snapshot()['voice_ready'])
        self.controller.start_call().result(2)
        self.assertFalse(self.controller.call.active)
        self.assertFalse(self.controller.call_requested)
        self.assertEqual(self.controller.call.starts, [])


class AdmissionTests(ReadinessBase):
    def test_startup_preloads_and_only_ready_event_allows_call(self):
        self.assertEqual(self.voice.prepare_calls, 1)
        self.assertTrue(self.controller.snapshot()['voice_preparing'])
        self.assert_blocked()
        self.assertEqual(self.chat.calls, 0)
        self.assertFalse(self.controller.pipeline.voice_enabled)
        self.voice.ready.set()
        self.controller.start_call().result(2)
        self.assertTrue(self.controller.call.active)
        self.assertTrue(self.controller.call_requested)
        self.assertEqual(len(self.controller.call.starts), 1)

    def test_loading_defers_scheduled_and_test_incoming_calls(self):
        calls = self.controller.incoming_calls
        self.assertTrue(calls.busy(self.controller.snapshot()))
        self.assertIsNone(calls.ring({'reason':'fixture', 'opening':'我想和你聊聊。'}))
        self.assertIsNone(self.controller.test_incoming_call().result(2))
        self.ringtone.start.assert_not_called()
        self.voice.ready.set()
        self.assertFalse(calls.busy(self.controller.snapshot()))
        self.assertIsNotNone(self.controller.test_incoming_call().result(2))

    def test_unready_answer_keeps_incoming_unclaimed_and_ringing(self):
        calls = self.controller.incoming_calls
        identifier = calls.ring({'reason':'fixture', 'opening':'我想和你聊聊。'}, test=True)
        self.ringtone.stop.reset_mock()
        self.assertIsNone(self.controller.answer_call(identifier).result(2))
        self.assertEqual(calls.snapshot()['id'], identifier)
        self.assertIsNone(calls.connected_id)
        self.assertEqual(self.controller.call.starts, [])
        self.assertEqual(self.voice.jobs, [])
        self.ringtone.stop.assert_not_called()
        self.voice.ready.set()
        self.assertTrue(self.controller.answer_call(identifier).result(2))
        self.assertTrue(self.controller.pipeline.wait_finished(2))
        self.assertTrue(self.controller.call.active)

    def test_synchronous_failure_can_retry_without_connecting_automatically(self):
        self.voice.failure = True
        self.controller.prepare_local_voice()
        self.assertIsNotNone(self.controller.snapshot()['voice_prepare_error'])
        self.assert_blocked()
        self.voice.failure = False
        self.assertTrue(self.controller.retry_voice_prepare().result(2))
        self.assertEqual(self.voice.releases, 1)
        self.assertIsNone(self.controller.snapshot()['voice_prepare_error'])
        self.assertTrue(self.controller.snapshot()['voice_preparing'])
        self.voice.ready.set()
        self.assertTrue(self.controller.snapshot()['voice_ready'])
        self.assertFalse(self.controller.call.active)

    def test_async_failure_blocks_call_and_retry_during_loading_is_a_noop(self):
        self.assertFalse(self.controller.retry_voice_prepare().result(2))
        self.assertEqual(self.voice.prepare_calls, 1)
        self.voice.prepare_error = 'fixture model file missing'
        self.assertFalse(self.controller.snapshot()['voice_preparing'])
        self.assert_blocked()
        self.assertIn('fixture model file missing', self.controller.message)
        self.assertTrue(self.controller.retry_voice_prepare().result(2))
        self.assertIsNone(self.controller.snapshot()['voice_prepare_error'])

    def test_old_voice_ready_does_not_unlock_newly_selected_model(self):
        next_voice = LoadingVoice(); chat = FakeChat(); chat.outputs_audio = False
        self.router.backends['gpt'] = (chat, next_voice)
        self.controller.apply_preferences(replace(self.controller.preferences, engine='gpt')).result(2)
        self.assertEqual(next_voice.prepare_calls, 1)
        self.voice.ready.set()
        self.assert_blocked()
        next_voice.ready.set()
        self.assertTrue(self.controller.snapshot()['voice_ready'])

    def test_switching_active_or_muted_cloud_call_waits_for_local_voice(self):
        for muted in (False, True):
            self.controller.apply_preferences(replace(self.controller.preferences, engine='qwen')).result(2)
            self.controller.start_call().result(2)
            if muted: self.controller.mute().result(2)
            starts = len(self.controller.call.starts)
            self.controller.apply_preferences(replace(self.controller.preferences, engine='deepseek')).result(2)
            self.assertFalse(self.controller.call.active)
            self.assertFalse(self.controller.call_requested)
            self.assertFalse(self.controller.muted)
            self.assertEqual(len(self.controller.call.starts), starts)

    def test_voice_disappearing_during_prepare_never_opens_microphone(self):
        self.voice.ready.set()
        with patch.object(self.voice, 'prepare', side_effect=self.voice.ready.clear):
            self.assertIsNone(self.controller.start_call().result(2))
        self.assertEqual(self.controller.call.starts, [])
        self.assertFalse(self.controller.pipeline.voice_enabled)

    def test_muted_call_cannot_resume_with_unready_voice(self):
        self.voice.ready.set(); self.controller.start_call().result(2)
        self.controller.mute().result(2)
        self.voice.ready.clear(); starts = len(self.controller.call.starts)
        self.controller.mute().result(2)
        self.assertTrue(self.controller.muted)
        self.assertFalse(self.controller.call.active)
        self.assertEqual(len(self.controller.call.starts), starts)

    def test_worker_failure_ends_active_call(self):
        self.voice.ready.set(); self.controller.start_call().result(2)
        self.voice.ready.clear(); self.voice.prepare_error = 'fixture worker exited'
        self.controller.poll(); self.controller.executor.submit(lambda: None).result(2)
        self.assertFalse(self.controller.call.active)
        self.assertFalse(self.controller.call_requested)
        self.assertIn('重新加载语音', self.controller.message)

    def test_configuration_save_also_preloads_selected_voice(self):
        self.controller.apply_preferences(replace(self.controller.preferences, engine='qwen')).result(2)
        profile = self.controller.models.snapshot()['deepseek']
        with patch.object(self.controller.models, 'prepare', return_value=object()), \
                patch.object(self.controller.models, 'commit'):
            self.assertTrue(self.controller.configure_model('deepseek', profile.model, profile.url, 'fixture-key').result(2))
        self.assertGreater(self.voice.prepare_calls, 1)
        self.assert_blocked()


class WorkerStateTests(unittest.TestCase):
    def test_stale_ready_event_cannot_hide_dead_process(self):
        voice = LocalVoice(player=lambda *args: None)
        self.addCleanup(voice.close)
        voice._process = Mock(); voice._process.poll.return_value = 0
        voice.ready.set()
        self.assertFalse(voice.prepared)

    def test_current_worker_eof_invalidates_readiness_but_old_eof_does_not(self):
        voice = LocalVoice(player=lambda *args: None)
        self.addCleanup(voice.close)
        voice._process = Mock(stdout=io.StringIO('')); voice._process.poll.return_value = None
        voice.ready.set()
        voice._read(Mock(stdout=io.StringIO('')))
        self.assertTrue(voice.prepared)
        voice._read(voice._process)
        self.assertFalse(voice.ready.is_set())
        self.assertFalse(voice.prepared)
        self.assertIn('重新加载语音', voice.prepare_error)


class ReadinessUiTests(ReadinessBase):
    # Only the explicitly defined UI case; controller cases run above.
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_phone_disables_call_until_ready_and_retries_failed_preload(self):
        from client_app import ClientWindow
        from PySide6.QtTest import QTest
        window = ClientWindow(self.controller, ROOT); window.show(); window.open_call()
        try:
            deadline = time.monotonic() + 3
            while window.overlay.phase != 'hidden' and time.monotonic() < deadline:
                QTest.qWait(15)
            self.assertEqual(window.overlay.phase, 'hidden')
            window.refresh()
            self.assertFalse(window.voice_button.isEnabled())
            self.assertEqual(window.voice_button.accessibleName(), '语音加载中')
            def button_pixel():
                image = window.grab().toImage(); dpr = window.devicePixelRatioF()
                x = round((window.offset_x+846*window.scale)*dpr)
                y = round((window.offset_y+690*window.scale)*dpr)
                return image.pixelColor(x,y).name()
            self.assertNotEqual(button_pixel(), '#37aa7a')
            with patch.object(window, 'label') as label:
                window.grab()
                self.assertIn('加载完成后可通话', [call.args[1] for call in label.call_args_list])
            directory = Path(os.environ.get('RONOVA_PREVIEW_DIR', ROOT/'runtime/verification-records/voice-call-ready/ui'))
            directory.mkdir(parents=True, exist_ok=True)
            self.assertTrue(window.grab().save(str(directory/'voice-loading.png')))
            self.voice.prepare_error = '模型文件缺失，请检查本地语音资源'; window.refresh()
            self.assertFalse(window.voice_button.isEnabled())
            self.assertEqual(window.status_button.accessibleName(), '重新加载语音')
            self.assertIn('模型文件缺失', window.status_button.toolTip())
            self.assertTrue(window.grab().save(str(directory/'voice-load-failed.png')))
            window.status_action(); self.controller.executor.submit(lambda: None).result(2)
            self.assertEqual(self.voice.releases, 1)
            self.voice.ready.set(); window.refresh()
            self.assertTrue(window.voice_button.isEnabled())
            self.assertEqual(window.voice_button.accessibleName(), '语音通话')
            self.assertEqual(button_pixel(), '#37aa7a')
            self.assertFalse(self.controller.call.active)
            window.voice(); self.controller.executor.submit(lambda: None).result(2)
            self.assertTrue(self.controller.call.active)
            window.refresh()
            self.assertTrue(window.grab().save(str(directory/'voice-ready-call.png')))
        finally:
            if window.tray: window.tray.hide()
            self.controller.close().result(5); window.close(); self.app.processEvents()


if __name__ == '__main__': unittest.main()
