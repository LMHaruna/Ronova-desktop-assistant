"""Workspace dictation and answer playback, one audible voice session at a time."""
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal


ECHO_GUARD_SECONDS = .35


class WorkspaceVoiceBusy(RuntimeError):
    """The companionship surfaces already hold the only audible session."""


class WorkspaceVoice(QObject):
    """Reuses the companion microphone, recogniser and voice engine.

    Dictation never submits by itself: recognised text lands in the composer so
    the user can check the wording before sending. Playback reuses the prepared
    companion voice, so the workspace never starts a second model or device.
    """

    recognized_partial = Signal(str)
    transcript_ready = Signal(str)
    listening_changed = Signal(bool)
    playback_changed = Signal(bool)
    preparation_changed = Signal(str)
    status_changed = Signal(str)
    _playback_finished = Signal(object)

    def __init__(self, controller, microphone_factory=None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.microphone_factory = microphone_factory
        self.microphone = None
        self.job = None
        self._echo_until = 0.
        self._lock = threading.RLock()
        self._preparation_timer = QTimer(self, interval=100)
        self._preparation_timer.timeout.connect(self._check_preparation)
        self._playback_finished.connect(self._finish_playback, Qt.ConnectionType.QueuedConnection)

    # -- shared companion voice ------------------------------------------------
    @property
    def backend(self):
        return getattr(getattr(self.controller, 'router', None), 'voice', None)

    def state(self):
        """Voice readiness comes from the companion model, never a second load."""
        load = getattr(self.controller, 'voice_load_state', None)
        if not callable(load):
            return {'voice_ready': False, 'voice_preparing': False, 'voice_prepare_error': '语音未配置'}
        try:
            return dict(load())
        except Exception as exc:
            return {'voice_ready': False, 'voice_preparing': False,
                    'voice_prepare_error': f'{type(exc).__name__}: {exc}'}

    def require_idle_session(self):
        call = getattr(self.controller, 'call', None)
        if getattr(call, 'active', False):
            raise WorkspaceVoiceBusy('陪伴通话进行中：先挂断，再使用工作区语音')
        pipeline = getattr(self.controller, 'pipeline', None)
        if getattr(pipeline, 'responding', False):
            raise WorkspaceVoiceBusy('陪伴系统正在回应：等这一轮结束，或先在手机里停止')

    @property
    def playback_active(self):
        with self._lock:
            job = self.job
        if job is not None and not job.finished.is_set():
            return True
        voice = self.backend
        return bool(getattr(voice, 'playback_active', False))

    @property
    def dictating(self):
        microphone = self.microphone
        return bool(microphone is not None and microphone.state != 'off')

    def _blocked(self):
        """Keep our own reply out of the transcript without muting the caller."""
        now = time.perf_counter()
        if self.playback_active:
            self._echo_until = now + ECHO_GUARD_SECONDS
            return True
        return now < self._echo_until

    # -- dictation -------------------------------------------------------------
    def microphone_device(self):
        return getattr(getattr(self.controller, 'preferences', None), 'microphone', None)

    def microphone_factory_for_use(self):
        if self.microphone_factory is not None:
            return self.microphone_factory
        from voice_input import LiveMicrophone
        return LiveMicrophone

    def start_dictation(self):
        self.require_idle_session()
        if self.dictating:
            return True
        self.stop_playback()
        from voice_input import CALL_CONFIG
        microphone = self.microphone_factory_for_use()(
            on_start=lambda: None,
            on_confirmed=lambda: self.status_changed.emit('已确认中文语音，继续说话'),
            on_partial=lambda text: self.recognized_partial.emit(text),
            on_final=lambda text, metrics: self.transcript_ready.emit(text),
            status=lambda text: self.status_changed.emit(text),
            blocked=self._blocked,
            config=CALL_CONFIG)
        self.microphone = microphone
        try:
            microphone.start(self.microphone_device())
        except Exception:
            self.microphone = None
            raise
        self.listening_changed.emit(True)
        return True

    def stop_dictation(self):
        microphone, self.microphone = self.microphone, None
        if microphone is not None:
            microphone.stop()
        self.listening_changed.emit(False)

    # -- answer playback -------------------------------------------------------
    def speak_answer(self, text):
        text = ' '.join((text or '').split())
        if not text:
            return False
        self.require_idle_session()
        state = self.state()
        if not state.get('voice_ready'):
            error = state.get('voice_prepare_error')
            if error:
                self._clear_preparation()
                self.status_changed.emit(f'语音模型未就绪，文字回答可继续：{error}')
                return False
            voice = self.backend
            prepare = getattr(voice, 'prepare', None)
            self._show_preparation('语音模型未就绪，文字回答可继续，正在加载…')
            if callable(prepare):
                try:
                    prepare()
                except Exception as exc:
                    self._clear_preparation()
                    self.status_changed.emit(f'语音未启动（{type(exc).__name__}），文字回答可继续')
            return False
        voice = self.backend
        if voice is None:
            self.status_changed.emit('语音未配置，文字回答可继续')
            return False
        self.stop_playback()
        try:
            job = voice.speak(text)
        except Exception as exc:
            self.status_changed.emit(f'播报未启动（{type(exc).__name__}），文字回答可继续')
            return False
        if job is None:
            self.status_changed.emit('播报未启动，文字回答可继续')
            return False
        with self._lock:
            self.job = job
        self.playback_changed.emit(True)
        self._show_preparation('正在准备语音…')
        threading.Thread(target=self._wait_playback, args=(job,), daemon=True).start()
        return True

    def _show_preparation(self, text):
        self._preparation_timer.start()
        self.preparation_changed.emit(text)

    def _clear_preparation(self):
        if self._preparation_timer.isActive():
            self._preparation_timer.stop()
            self.preparation_changed.emit('')

    def _check_preparation(self):
        with self._lock:
            job = self.job
        if job is not None:
            if not job.finished.is_set() and 'first_playback_seconds' in job.metrics:
                self._clear_preparation()
                self.status_changed.emit('正在播报回答')
            return
        state = self.state()
        error = state.get('voice_prepare_error')
        if state.get('voice_ready') or error:
            self._clear_preparation()
            if error:
                self.status_changed.emit(f'语音模型未就绪，文字回答可继续：{error}')

    def _wait_playback(self, job):
        job.finished.wait()
        self._playback_finished.emit(job)

    def _finish_playback(self, job):
        with self._lock:
            if self.job is not job:
                return
            self.job = None
        self._clear_preparation()
        self.playback_changed.emit(False)
        error = getattr(job, 'error', None)
        if error:
            self.status_changed.emit(f'播报中断：{error}')

    def stop_playback(self):
        with self._lock:
            job, self.job = self.job, None
        self._clear_preparation()
        voice = self.backend
        if voice is not None:
            try:
                voice.stop()
            except Exception:
                pass
        if job is not None:
            try:
                job.cancelled.set()
            except Exception:
                pass
        if job is not None:
            self.playback_changed.emit(False)

    def close(self):
        self.stop_dictation()
        self.stop_playback()
