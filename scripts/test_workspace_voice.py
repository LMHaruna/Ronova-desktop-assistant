"""Offline checks for workspace dictation, answer playback and voice arbitration."""
import os
import shutil
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from client_codex import CodexAppServer
from client_workspace import WorkspacePane, WorkspaceTurn
from client_workspace_voice import WorkspaceVoice, WorkspaceVoiceBusy


@contextmanager
def scratch():
    """Workspace-local scratch when the shell is file-sandboxed, temp otherwise."""
    root = os.environ.get('RONOVA_TEST_SCRATCH')
    if root:
        path = Path(root)/uuid.uuid4().hex
        path.mkdir(parents=True)
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)
    else:
        with TemporaryDirectory() as directory:
            yield Path(directory)


class FakeJob:
    def __init__(self):
        self.finished = threading.Event()
        self.cancelled = threading.Event()
        self.error = None
        self.metrics = {}


class FakeVoice:
    def __init__(self):
        self.spoken = []
        self.stops = 0
        self.prepares = 0
        self.playback_active = False

    def prepare(self):
        self.prepares += 1

    def speak(self, text):
        job = FakeJob()
        self.spoken.append((text, job))
        self.playback_active = True
        return job

    def stop(self):
        self.stops += 1
        self.playback_active = False
        for _, job in self.spoken:
            if not job.finished.is_set():
                job.cancelled.set()
                job.finished.set()

    def finish_latest(self):
        _, job = self.spoken[-1]
        job.finished.set()
        self.playback_active = False
        return job


class FakeMicrophone:
    """Stands in for LiveMicrophone: records callbacks instead of opening audio."""
    instances = []

    def __init__(self, **callbacks):
        self.callbacks = callbacks
        self.state = 'off'
        self.started = []
        self.stopped = 0
        self.blocked_calls = 0
        FakeMicrophone.instances.append(self)

    def start(self, device=None):
        self.started.append(device)
        self.state = 'listening'
        if self.callbacks['blocked']():
            self.blocked_calls += 1

    def stop(self):
        self.stopped += 1
        self.state = 'off'

    def final(self, text, metrics=None):
        self.callbacks['on_final'](text, metrics or {})

    def partial(self, text):
        self.callbacks['on_partial'](text)


class FakeController:
    def __init__(self, ready=True):
        self.preferences = SimpleNamespace(microphone='mic-1')
        self.voice = FakeVoice()
        self.router = SimpleNamespace(voice=self.voice)
        self.call = SimpleNamespace(active=False)
        self.pipeline = SimpleNamespace(responding=False)
        self.ready = ready
        self.state_reads = 0

    def voice_load_state(self):
        self.state_reads += 1
        return {'voice_ready': self.ready, 'voice_preparing': not self.ready,
                'voice_prepare_error': None}


def wait_for(predicate, timeout=2.0):
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if app is not None:
            QTest.qWait(10)
        else:
            time.sleep(.01)
    return predicate()


class DictationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        FakeMicrophone.instances.clear()

    def make(self, **options):
        controller = FakeController(**options)
        return controller, WorkspaceVoice(controller, microphone_factory=FakeMicrophone)

    def test_dictation_uses_the_companion_microphone_and_device(self):
        controller, voice = self.make()
        partials, finals = [], []
        voice.recognized_partial.connect(partials.append)
        voice.transcript_ready.connect(finals.append)
        self.assertTrue(voice.start_dictation())
        microphone = FakeMicrophone.instances[-1]
        self.assertEqual(microphone.started, ['mic-1'])
        self.assertTrue(voice.dictating)
        microphone.partial('这个报错')
        microphone.final('这个报错是什么意思', {'voiced_ms': 900})
        self.assertEqual(partials, ['这个报错'])
        self.assertEqual(finals, ['这个报错是什么意思'])
        voice.stop_dictation()
        self.assertEqual(microphone.stopped, 1)
        self.assertFalse(voice.dictating)

    def test_dictation_stops_reply_playback_before_listening(self):
        controller, voice = self.make()
        voice.speak_answer('上一段回答')
        self.assertTrue(controller.voice.playback_active)
        voice.start_dictation()
        self.assertGreaterEqual(controller.voice.stops, 1)
        self.assertFalse(controller.voice.playback_active)

    def test_companion_call_keeps_the_only_voice_session(self):
        controller, voice = self.make()
        controller.call.active = True
        with self.assertRaises(WorkspaceVoiceBusy):
            voice.start_dictation()
        with self.assertRaises(WorkspaceVoiceBusy):
            voice.speak_answer('不该播出去')
        self.assertEqual(controller.voice.spoken, [])
        self.assertEqual(FakeMicrophone.instances, [])

    def test_companion_reply_in_progress_also_blocks_workspace_voice(self):
        controller, voice = self.make()
        controller.pipeline.responding = True
        with self.assertRaises(WorkspaceVoiceBusy):
            voice.start_dictation()


class PlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, **options):
        controller = FakeController(**options)
        return controller, WorkspaceVoice(controller, microphone_factory=FakeMicrophone)

    def test_answer_is_spoken_with_the_companion_voice_and_reports_finish(self):
        controller, voice = self.make()
        playing = []
        voice.playback_changed.connect(playing.append)
        self.assertTrue(voice.speak_answer('第一段\n\n  第二段 '))
        self.assertEqual(controller.voice.spoken[0][0], '第一段 第二段')
        self.assertEqual(playing, [True])
        controller.voice.finish_latest()
        self.assertTrue(wait_for(lambda: playing == [True, False]))
        self.assertFalse(voice.playback_active)

    def test_unready_voice_keeps_the_text_and_asks_the_model_to_load(self):
        controller, voice = self.make(ready=False)
        self.addCleanup(voice.close)
        notes = []
        voice.preparation_changed.connect(notes.append)
        self.assertFalse(voice.speak_answer('你好'))
        self.assertEqual(controller.voice.spoken, [])
        self.assertEqual(controller.voice.prepares, 1)
        self.assertIn('文字回答可继续', notes[-1])
        controller.ready = True
        self.assertTrue(wait_for(lambda: notes[-1] == ''))

    def test_prepare_failure_is_reported_without_starting_playback(self):
        controller, voice = self.make(ready=False)
        controller.voice_load_state = lambda: {'voice_ready': False, 'voice_preparing': False,
                                               'voice_prepare_error': '模型缺失'}
        notes = []
        voice.status_changed.connect(notes.append)
        self.assertFalse(voice.speak_answer('你好'))
        self.assertIn('模型缺失', notes[-1])
        self.assertEqual(controller.voice.prepares, 0)

    def test_speech_engine_failure_leaves_text_usable(self):
        controller, voice = self.make()
        controller.voice.speak = lambda text: (_ for _ in ()).throw(RuntimeError('无音频设备'))
        notes = []
        voice.status_changed.connect(notes.append)
        self.assertFalse(voice.speak_answer('你好'))
        self.assertIn('RuntimeError', notes[-1])

    def test_stopping_playback_cancels_the_job_and_announces_once(self):
        controller, voice = self.make()
        playing = []
        voice.playback_changed.connect(playing.append)
        voice.speak_answer('长回答')
        voice.stop_playback()
        self.assertEqual(playing, [True, False])
        self.assertTrue(controller.voice.spoken[0][1].cancelled.is_set())
        self.assertFalse(voice.playback_active)

    def test_blank_answer_never_opens_the_voice_engine(self):
        controller, voice = self.make()
        self.assertFalse(voice.speak_answer('  \n '))
        self.assertEqual(controller.voice.spoken, [])


class WorkspaceVoicePaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        FakeMicrophone.instances.clear()

    def open_workspace(self, **options):
        directory = scratch()
        root = directory.__enter__()
        self.addCleanup(directory.__exit__, None, None, None)
        server = CodexAppServer(root/'codex')
        self.addCleanup(server.close)
        controller = FakeController(**options)
        voice = WorkspaceVoice(controller, microphone_factory=FakeMicrophone)
        self.addCleanup(voice.close)
        pane = WorkspacePane(QPixmap(), '', lambda: None, server, lambda: None, voice)
        self.addCleanup(pane.close)
        pane.resize(920, 700)
        pane.show()
        return controller, voice, pane

    def test_transcript_waits_in_the_composer_instead_of_sending(self):
        controller, voice, pane = self.open_workspace()
        QTest.mouseClick(pane.mic, Qt.MouseButton.LeftButton)
        microphone = FakeMicrophone.instances[-1]
        self.assertTrue(pane.mic.isChecked())
        self.assertIsNone(pane.request)
        microphone.final('解释这个报错')
        self.assertEqual(pane.input.toPlainText(), '解释这个报错')
        self.assertIsNone(pane.request)
        microphone.final('并给出改法')
        self.assertEqual(pane.input.toPlainText(), '解释这个报错 并给出改法')
        QTest.mouseClick(pane.mic, Qt.MouseButton.LeftButton)
        self.assertFalse(pane.mic.isChecked())
        self.assertEqual(microphone.stopped, 1)

    def test_partial_transcript_updates_the_status_line_only(self):
        controller, voice, pane = self.open_workspace()
        QTest.mouseClick(pane.mic, Qt.MouseButton.LeftButton)
        FakeMicrophone.instances[-1].partial('慢一点')
        self.assertIn('识别中：慢一点', pane.status.text())
        self.assertEqual(pane.input.toPlainText(), '')

    def test_microphone_refuses_while_a_companion_call_is_active(self):
        controller, voice, pane = self.open_workspace()
        controller.call.active = True
        QTest.mouseClick(pane.mic, Qt.MouseButton.LeftButton)
        self.assertFalse(pane.mic.isChecked())
        self.assertIn('陪伴通话进行中', pane.status.text())

    def test_hiding_the_workspace_closes_the_microphone(self):
        controller, voice, pane = self.open_workspace()
        QTest.mouseClick(pane.mic, Qt.MouseButton.LeftButton)
        microphone = FakeMicrophone.instances[-1]
        pane.hide()
        QTest.qWait(10)
        self.assertEqual(microphone.stopped, 1)
        self.assertFalse(pane.mic.isChecked())

    def test_microphone_is_between_model_and_send(self):
        _, _, pane = self.open_workspace()
        QTest.qWait(10)
        self.assertLess(pane.model_button.geometry().right(), pane.mic.x())
        self.assertLess(pane.mic.geometry().right(), pane.send.x())
        self.assertIsNone(pane.mic.parentWidget().findChild(QPushButton, 'workSpeaker'))

    def test_each_reply_button_plays_its_own_text_and_can_stop(self):
        controller, _, pane = self.open_workspace()
        first = WorkspaceTurn('问题一', state='done', items={
            'note': {'type': 'agentMessage', 'phase': 'commentary', 'text': '处理进度'},
            'command': {'type': 'commandExecution', 'command': 'Get-Location'},
            'reply': {'type': 'agentMessage', 'text': '**第一条回复**'},
        })
        second = WorkspaceTurn('问题二', state='done', items={
            'reply': {'type': 'agentMessage', 'text': '第二条回复'},
        })
        pane.active_chat.turns.extend((first, second))
        pane.render_history()
        self.assertEqual(controller.voice.spoken, [])
        a, b = (pane.history.rows[turn] for turn in (first, second))
        for row in (a, b):
            self.assertTrue(row.speaker.isVisible())
            self.assertEqual(row.speaker.y() - row.answer.geometry().bottom() - 1, 1)
        QTest.mouseClick(a.speaker, Qt.MouseButton.LeftButton)
        self.assertEqual(controller.voice.spoken[0][0], '第一条回复')
        self.assertEqual(a.speaker.accessibleName(), '停止播放这条回复')
        self.assertEqual(pane.input.placeholderText(), '正在准备语音…')
        self.assertEqual(pane.status.text(), '')
        pane.input.setPlainText('保留草稿')
        QTest.mouseClick(b.speaker, Qt.MouseButton.LeftButton)
        self.assertEqual(controller.voice.spoken[1][0], '第二条回复')
        self.assertTrue(controller.voice.spoken[0][1].cancelled.is_set())
        self.assertEqual(a.speaker.accessibleName(), '播放这条回复')
        self.assertEqual(b.speaker.accessibleName(), '停止播放这条回复')
        QTest.mouseClick(b.speaker, Qt.MouseButton.LeftButton)
        self.assertTrue(controller.voice.spoken[1][1].cancelled.is_set())
        self.assertEqual(b.speaker.accessibleName(), '播放这条回复')
        self.assertEqual(pane.input.toPlainText(), '保留草稿')
        pane.input.clear()
        self.assertEqual(pane.input.placeholderText(), '和若娜瓦一起工作…')

    def test_restored_reply_can_play_and_button_resets_on_completion(self):
        controller, _, pane = self.open_workspace()
        pane.codex_history_loaded(pane.active_chat, {'data': [{
            'id': 'old-turn', 'status': 'completed', 'items': [
                {'type': 'userMessage', 'content': [{'type': 'text', 'text': '旧问题'}]},
                {'type': 'agentMessage', 'id': 'reply', 'text': '旧回复'},
            ],
        }]}, '')
        row = pane.history.rows[pane.turns[0]]
        QTest.mouseClick(row.speaker, Qt.MouseButton.LeftButton)
        self.assertEqual(controller.voice.spoken[0][0], '旧回复')
        controller.voice.spoken[0][1].metrics['first_playback_seconds'] = .2
        self.assertTrue(wait_for(lambda: pane.status.text() == '正在播报回答'))
        self.assertEqual(pane.input.placeholderText(), '和若娜瓦一起工作…')
        controller.voice.finish_latest()
        self.assertTrue(wait_for(lambda: row.speaker.accessibleName() == '播放这条回复'))

    def test_streaming_and_process_notes_have_no_play_button(self):
        controller, _, pane = self.open_workspace()
        turn = WorkspaceTurn('问题', items={
            'note': {'type': 'agentMessage', 'phase': 'commentary', 'text': '处理中'},
        })
        pane.active_chat.turns.append(turn)
        pane.render_history()
        row = pane.history.rows[turn]
        self.assertTrue(row.answer.isVisible())
        self.assertFalse(row.speaker.isVisible())
        turn.state = 'done'
        pane.render_history()
        self.assertFalse(row.speaker.isVisible())
        self.assertEqual(controller.voice.spoken, [])


if __name__ == '__main__':
    unittest.main()
