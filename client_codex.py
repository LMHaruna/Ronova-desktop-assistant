"""Local Codex App Server connection and ChatGPT browser sign-in."""
import json
from pathlib import Path
import platform
import re
import shutil
import sys

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices


def codex_executable():
    executable = shutil.which('codex.exe' if sys.platform == 'win32' else 'codex')
    if sys.platform != 'win32':
        return executable
    environment = QProcessEnvironment.systemEnvironment()
    windows_apps = Path(environment.value('ProgramFiles', r'C:\Program Files')) / 'WindowsApps'
    # PATH can still point to the previous desktop package after an update.
    if executable and windows_apps not in Path(executable).parents:
        return executable
    candidates = []
    architecture = 'arm64' if platform.machine().lower() == 'arm64' else 'x64'
    try:
        for package in windows_apps.glob(f'OpenAI.Codex_*_{architecture}__2p2nqsd0c76g0'):
            match = re.fullmatch(r'OpenAI\.Codex_(\d+(?:\.\d+){3})_.*', package.name, re.I)
            binary = package / 'app/resources/codex.exe'
            if match and binary.is_file():
                candidates.append((tuple(map(int, match[1].split('.'))), binary))
    except OSError:
        pass
    if candidates:
        return str(max(candidates, key=lambda entry: entry[0])[1])
    if executable:
        return executable
    local = environment.value('LOCALAPPDATA')
    if local:
        bundled = Path(local) / 'OpenAI' / 'Codex' / 'bin'
        try:
            return str(max(bundled.glob('*/codex.exe'), key=lambda path: path.stat().st_mtime))
        except (OSError, ValueError):
            pass
    return None


class CodexAppServer(QObject):
    ready = Signal()
    disconnected = Signal(str)
    account_changed = Signal()
    event = Signal(str, dict)
    approval = Signal(object, str, dict)

    def __init__(self, home, parent=None):
        super().__init__(parent)
        self.home = Path(home).resolve()
        self.default_cwd = self.home / 'workspace'
        self.media = self.home / 'screenshots'
        self.process = QProcess(self)
        self.process.started.connect(self.initialize)
        self.process.readyReadStandardOutput.connect(self.read_messages)
        self.process.readyReadStandardError.connect(self.process.readAllStandardError)
        self.process.finished.connect(self.process_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.connected = self.closing = False
        self.failing = False
        self.account = None
        self.login_id = None
        self.login_busy = False
        self.message = ''
        self.buffer = b''
        self.sequence = 0
        self.pending = {}
        self.loaded = set()

    @property
    def signed_in(self):
        return bool(self.account and self.account.get('type') == 'chatgpt')

    @property
    def connecting(self):
        return not self.connected and self.process.state() != QProcess.ProcessState.NotRunning

    def start(self):
        if self.closing or self.connected or self.connecting:
            return
        executable = codex_executable()
        if not executable:
            self.fail('找不到 Codex。请安装 Codex 桌面客户端，或将 Codex CLI 可执行文件加入 PATH。')
            return
        try:
            self.default_cwd.mkdir(parents=True, exist_ok=True)
            self.media.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.fail('无法建立若娜瓦的 Codex 数据目录。')
            return
        environment = QProcessEnvironment.systemEnvironment()
        for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN',
                    'OPENAI_BASE_URL', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE'):
            environment.remove(key)
        environment.insert('CODEX_HOME', str(self.home))
        self.process.setProcessEnvironment(environment)
        self.process.setWorkingDirectory(str(self.default_cwd))
        self.message = ''
        self.buffer = b''
        args = ['app-server', '--listen', 'stdio://', '-c', 'cli_auth_credentials_store="file"']
        if sys.platform == 'win32':
            args.extend(['-c', 'features.prefer_mxc=true', '-c', 'windows.sandbox="unelevated"'])
        self.process.start(executable, args)
        self.account_changed.emit()

    def initialize(self):
        self.call('initialize', {'clientInfo': {'name': 'ronova', 'title': 'Ronova', 'version': '1.0'},
                                 'capabilities': {'experimentalApi': True}},
                  self.initialized, handshake=True)

    def initialized(self, _result, error):
        if error:
            self.fail(error)
            return
        if not self.send({'method': 'initialized'}):
            return
        self.connected = True
        self.read_account()
        self.ready.emit()

    def send(self, message):
        if self.process.state() != QProcess.ProcessState.Running:
            self.fail('Codex 连接已断开，请重新连接。')
            return False
        line = (json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        if self.process.write(line) != len(line):
            self.fail('无法向 Codex 发送请求。请重试。')
            return False
        return True

    def call(self, method, params, callback, *, handshake=False, disconnect_on_timeout=True):
        if self.closing or not (self.connected or handshake):
            callback({}, self.message or 'Codex 正在连接，请稍后重试。')
            return
        self.sequence += 1
        request_id = self.sequence
        timer = QTimer(self, singleShot=True, interval=30000)
        if disconnect_on_timeout:
            timer.timeout.connect(lambda: self.fail('等待 Codex 响应超时，请重新连接。'))
        else:
            timer.timeout.connect(lambda: self.expire(request_id))
        self.pending[request_id] = (callback, timer)
        timer.start()
        self.send({'id': request_id, 'method': method, 'params': params})

    def expire(self, request_id):
        pending = self.pending.pop(request_id,None)
        if pending is None:
            return
        callback,timer = pending
        timer.deleteLater()
        callback({},'等待 Codex 额度响应超时。')

    def read_messages(self):
        self.buffer += bytes(self.process.readAllStandardOutput())
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            if line.strip():
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    self.fail('Codex 返回了无法读取的数据。')
                    return
                self.dispatch(message)

    def dispatch(self, message):
        method = message.get('method')
        params = message.get('params') or {}
        request_id = message.get('id')
        if method and request_id is not None:
            if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                          'item/tool/requestUserInput'):
                self.approval.emit(request_id, method, params)
            else:
                self.send({'id': request_id, 'error': {'code': -32601, 'message': 'Unsupported request'}})
        elif method == 'account/login/completed':
            self.login_completed(params)
        elif method == 'account/updated':
            self.read_account()
        elif method:
            self.event.emit(method, params)
        elif request_id in self.pending:
            callback, timer = self.pending.pop(request_id)
            timer.stop()
            timer.deleteLater()
            error = message.get('error') or {}
            callback(message.get('result') or {}, str(error.get('message', ''))[:800])

    def read_account(self):
        self.call('account/read', {'refreshToken': False}, self.account_read)

    def account_read(self, result, error):
        self.account = result.get('account')
        if not self.login_busy:
            self.message = error
        self.account_changed.emit()

    def login(self):
        if not self.connected or self.login_busy:
            self.start()
            return
        self.login_busy = True
        self.message = ''
        self.account_changed.emit()
        self.call('account/login/start', {'type': 'chatgpt'}, self.login_started)

    def login_started(self, result, error):
        self.login_id = result.get('loginId')
        url = QUrl(result.get('authUrl', ''))
        if (not error and self.login_id and url.scheme() == 'https'
                and url.host().lower() in ('auth.openai.com', 'chatgpt.com')
                and QDesktopServices.openUrl(url)):
            self.account_changed.emit()
            return
        if self.login_id:
            self.call('account/login/cancel', {'loginId': self.login_id}, lambda *_: None)
        self.login_id = None
        self.login_busy = False
        self.message = error or '无法打开 ChatGPT 官方登录页面，请重试。'
        self.account_changed.emit()

    def cancel_login(self):
        if self.login_id:
            self.call('account/login/cancel', {'loginId': self.login_id}, self.login_cancelled)

    def login_cancelled(self, _result, error):
        if error:
            self.message = error
        else:
            self.login_id = None
            self.login_busy = False
            self.message = ''
        self.account_changed.emit()

    def login_completed(self, params):
        if not self.login_id or params.get('loginId') != self.login_id:
            return
        self.login_id = None
        self.login_busy = False
        if params.get('success'):
            self.read_account()
        else:
            self.message = 'ChatGPT 登录未完成，请重新登录。'
            self.account_changed.emit()

    def decide(self, request_id, decision):
        return self.send({'id': request_id, 'result': {'decision': decision}})

    def answer_questions(self, request_id, answers):
        return self.send({'id': request_id, 'result': {'answers': answers}})

    def process_finished(self, *_):
        if not self.closing and (self.connected or self.pending or not self.message):
            self.fail('Codex 连接已断开，请重新连接。')

    def process_error(self, _error):
        if not self.closing and not self.message:
            self.fail('无法启动或连接本机 Codex，请检查 Codex CLI 是否可运行。')

    def fail(self, message):
        if self.closing or self.failing:
            return
        self.failing = True
        self.connected = False
        self.account = self.login_id = None
        self.login_busy = False
        self.message = message
        self.loaded.clear()
        pending, self.pending = self.pending, {}
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
        for callback, timer in pending.values():
            timer.stop()
            timer.deleteLater()
            callback({}, message)
        self.disconnected.emit(message)
        self.account_changed.emit()
        self.failing = False

    def close(self):
        self.closing = True
        for _, timer in self.pending.values():
            timer.stop()
            timer.deleteLater()
        self.pending.clear()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.closeWriteChannel()
            if not self.process.waitForFinished(700):
                self.process.kill()
                self.process.waitForFinished(500)
