"""Render Ronova's video-call model into the phone's existing painter scene."""
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

from PySide6.QtGui import QImage, QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PySide6.QtOpenGL import QOpenGLFramebufferObject

MODEL_PATH = Path('design/video-call-live2d/export/ronova-call-v8-face-work/ronova-call-v8-face-work.model3.json')
PARAMETERS = ('ParamEyeLOpen', 'ParamEyeROpen', 'ParamMouthOpenY', 'ParamBreath')
BACKGROUND = (16 / 255, 17 / 255, 22 / 255, 1.)
REQUEST = struct.Struct('<IIIf')
RESPONSE = struct.Struct('<III')
RGBA = QImage.Format.Format_RGBA8888


def check_model(path):
    try:
        refs = json.loads(path.read_text(encoding='utf-8'))['FileReferences']
        resources = [refs['Moc'], *refs['Textures']]
        if not all((path.parent / name).is_file() for name in resources):
            raise FileNotFoundError()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('若娜瓦 Live2D 模型或纹理缺失，无法开启视频') from exc


class Live2DScene:
    def __init__(self, root, path, surface):
        dependency = str(root / 'runtime/live2d-prototype-deps')
        if dependency not in sys.path:
            sys.path.insert(0, dependency)
        try:
            import live2d
        except (ImportError, OSError) as exc:
            raise RuntimeError('Live2D 运行依赖无法加载，请检查客户端的运行环境') from exc

        self.sdk = live2d
        self.path = path
        self.model = self.buffer = None
        self.initialized = False
        self.started = self.previous = time.monotonic()
        self.mouth = 0.
        self.context = QOpenGLContext()
        self.context.setFormat(surface.format())
        self.surface = surface
        try:
            if not self.context.create():
                raise RuntimeError('无法创建视频通话的 OpenGL 上下文')
            self.make_current()
            try:
                live2d.init()
                self.initialized = True
                live2d.setLogLevel(live2d.LogLevels.LV_WARN)
                live2d.glInit()
            finally:
                self.context.doneCurrent()
        except Exception:
            self.close()
            raise

    def make_current(self):
        if not self.surface.isValid() or not self.context.makeCurrent(self.surface):
            raise RuntimeError('视频通话的 OpenGL 渲染环境不可用')

    def render(self, width, height, level):
        # Preserve the portrait's proportions. The phone painter crops the
        # sides of this 2:3 render to fill the narrower rounded screen.
        width = max(width, math.ceil(height * 2 / 3))
        height = max(height, math.ceil(width * 3 / 2))
        self.make_current()
        try:
            if self.model is None:
                model = self.sdk.Model()
                model.LoadModelJson(str(self.path))
                if not model.IsV3() or not set(PARAMETERS).issubset(model.GetParamIds()):
                    raise RuntimeError('若娜瓦模型缺少视频通话所需的绑定参数')
                model.SetAutoBlink(False)
                model.SetAutoBreath(False)
                self.model = model
            if self.buffer is None or (self.buffer.width(), self.buffer.height()) != (width, height):
                self.buffer = QOpenGLFramebufferObject(
                    width, height, QOpenGLFramebufferObject.Attachment.CombinedDepthStencil)
                if not self.buffer.isValid():
                    raise RuntimeError('无法创建 Live2D 视频画布')
                self.model.Resize(width, height)
            if not self.buffer.bind():
                raise RuntimeError('无法绑定 Live2D 视频画布')
            self.context.functions().glViewport(0, 0, width, height)
            self.sdk.clearBuffer(*BACKGROUND)
            now = time.monotonic()
            dt = min(now - self.previous, .1)
            self.previous = now
            elapsed = now - self.started
            blink_phase = elapsed % 4.7 - 2.1
            eye = 1. - math.sin(math.pi * blink_phase / .18) if 0. < blink_phase < .18 else 1.
            breath = .5 - .5 * math.cos(elapsed * math.tau / 4.6)
            self.mouth = max(0., min(1., level))
            for name, value in zip(PARAMETERS, (eye, eye, self.mouth, breath)):
                self.model.SetParamById(name, value)
            self.model.SaveParameters()
            self.model.Update(dt)
            self.model.Draw()
            frame = self.buffer.toImage()
            if frame.isNull():
                raise RuntimeError('Live2D 未返回可显示的画面')
            return frame
        finally:
            self.context.doneCurrent()

    def close(self):
        if self.initialized:
            current = self.surface.isValid() and self.context.makeCurrent(self.surface)
            try:
                if current and self.model is not None:
                    self.model.DestroyRenderer()
                self.buffer = self.model = None
                self.sdk.dispose()
            finally:
                self.initialized = False
                if current: self.context.doneCurrent()


class VideoRenderer:
    """Keep the native SDK in a separate process and only publish the latest frame."""
    def __init__(self, root):
        root = Path(root)
        model = root / MODEL_PATH
        check_model(model)
        python = Path(sys.executable)
        if python.name.lower() == 'pythonw.exe': python = python.with_name('python.exe')
        self.process = subprocess.Popen(
            [str(python), '-u', __file__, str(root), str(model)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.condition = threading.Condition()
        self.request = self.result = None
        self.generation = 0
        self.closed = self.processing = False
        self.thread = threading.Thread(target=self.run, name='Ronova-Live2D', daemon=True)
        self.thread.start()

    def step(self, width, height, level):
        with self.condition:
            result, self.result = self.result, None
            if not self.closed and (result is None or result[1] is None):
                self.request = (self.generation, width, height, level)
                self.condition.notify()
            return result

    def stop(self):
        with self.condition:
            self.generation += 1
            self.request = self.result = None

    def shutdown(self):
        with self.condition:
            self.closed = True
            self.condition.notify()
            if self.processing and self.process.poll() is None:
                self.process.terminate()
        return not self.thread.is_alive()

    def receive_frame(self, request):
        self.process.stdin.write(REQUEST.pack(*request))
        self.process.stdin.flush()
        width, height, count = RESPONSE.unpack(self.process.stdout.read(RESPONSE.size))
        if not 0 < count <= 16*1024*1024 or width and count != width*height*4:
            raise OSError('Invalid Live2D frame size')
        data = self.process.stdout.read(count)
        if len(data) != count: raise EOFError()
        if width == 0: return None, data.decode('utf-8')
        return QImage(data, width, height, width*4, RGBA).copy(), None

    def run(self):
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.request is not None or self.closed)
                    if self.closed: return
                    request, self.request = self.request, None
                    self.processing = True
                try:
                    result = self.receive_frame(request)
                except (OSError, EOFError, ValueError, struct.error):
                    if self.process.poll() is None: self.process.terminate()
                    result = (None, 'Live2D 渲染进程已退出，请重新发起视频通话')
                with self.condition:
                    self.processing = False
                    if request[0] == self.generation and not self.closed:
                        self.result = result
                        if result[1] is not None: self.request = None
        finally:
            try: self.process.stdin.close()
            except OSError: pass
            self.process.stdout.close()
            try: self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def render_worker(root, model, output):
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
    fmt.setAlphaBufferSize(8)
    fmt.setDepthBufferSize(24)
    fmt.setStencilBufferSize(8)
    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    scene = None
    active_generation = None
    try:
        while raw := sys.stdin.buffer.read(REQUEST.size):
            generation, width, height, level = REQUEST.unpack(raw)
            try:
                if scene is None: scene = Live2DScene(root, model, surface)
                first_frame = generation != active_generation
                if first_frame: scene.started = scene.previous = time.monotonic()
                frame = scene.render(width, height, level).convertToFormat(RGBA)
                if first_frame:
                    samples = {frame.pixel(frame.width()//2, round(frame.height()*y)) for y in (.05,.3,.48,.8)}
                    if len(samples) < 2:
                        raise RuntimeError('Live2D 首帧没有显示若娜瓦，请重新发起视频通话')
                    active_generation = generation
                data = frame.constBits()
                output.write(RESPONSE.pack(frame.width(), frame.height(), len(data)))
            except Exception as exc:
                message = str(exc) if isinstance(exc, RuntimeError) else 'Live2D 画面无法渲染'
                data = message.encode('utf-8')
                output.write(RESPONSE.pack(0, 0, len(data)))
            output.write(data)
            output.flush()
    finally:
        if scene is not None: scene.close()
        surface.destroy()
        app.quit()


if __name__ == '__main__':
    # Keep native SDK logging away from the binary frame pipe.
    with os.fdopen(os.dup(sys.stdout.fileno()), 'wb') as frames:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        render_worker(Path(sys.argv[1]), Path(sys.argv[2]), frames)
