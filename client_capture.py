"""Native desktop capture and a cancellable, per-monitor selection overlay."""
from dataclasses import dataclass
import math
import sys

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from client_theme import COLORS, themed
from client_workspace_controls import tool_icon


@dataclass(frozen=True)
class ScreenFrame:
    geometry: QRect
    image: QImage

    @property
    def pixels(self):
        # On Windows, Qt keeps the monitor origin in native desktop pixels
        # while scaling its logical size for high-DPI widget placement.
        return QRect(self.geometry.topLeft(), self.image.size())


def desktop_pointer():
    if sys.platform == 'win32':
        import ctypes
        from ctypes.wintypes import POINT
        point = POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return QPoint(point.x, point.y)
    return QCursor.pos()


def selection_rect(start, end):
    return QRect(min(start.x(), end.x()), min(start.y(), end.y()),
                 abs(start.x()-end.x()), abs(start.y()-end.y()))


def crop_selection(frames, region):
    parts = [(frame, frame.pixels.intersected(region)) for frame in frames if frame.pixels.intersects(region)]
    if not parts: return QImage()
    bounds = QRect()
    for _, rect in parts: bounds = bounds.united(rect)
    scale = min(1., 4096 / max(bounds.width(), bounds.height()))
    result = QImage(math.ceil(bounds.width()*scale), math.ceil(bounds.height()*scale), QImage.Format.Format_RGB32)
    if result.isNull(): raise RuntimeError('截图区域过大，请缩小选区')
    result.fill(QColor('#171A21'))
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, scale<1.)
    painter.scale(scale, scale)
    for frame, rect in parts:
        source = QRectF(rect.translated(-frame.pixels.topLeft()))
        target = QRectF(rect.translated(-bounds.topLeft()))
        painter.drawImage(target, frame.image, source)
    painter.end()
    return result


class SelectionOverlay(QWidget):
    def __init__(self, capture, screen, frame):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.capture = capture
        self.frame = frame
        self.setWindowTitle('框选截图')
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.winId()
        self.windowHandle().setScreen(screen)
        self.setGeometry(frame.geometry)
        self.toolbar = QFrame(self)
        self.toolbar.setObjectName('captureToolbar')
        self.toolbar.setStyleSheet(themed('''
            QFrame#captureToolbar { background:$surface; border:1px solid $border; border-radius:10px; }
            QLabel { color:$text; font-size:12px; }
            QPushButton { background:$elevated; border:0; border-radius:5px; padding:0; }
            QPushButton:hover { background:$selection; }
            QPushButton#confirmCapture { background:$primary; }
            QPushButton#confirmCapture:hover { background:$primaryHover; }
        '''))
        row = QHBoxLayout(self.toolbar); row.setContentsMargins(9,6,9,6); row.setSpacing(6)
        self.caption = QLabel(); row.addWidget(self.caption)
        self.caption.setToolTip('按空格保留当前选区，再框选下一张')
        for name, label, glyph, action in (
                ('resetCapture', '重选', '<path d="M4 10a8 8 0 1 1 1 7M4 4v6h6"/>', capture.reset),
                ('cancelCapture', '取消', '<path d="m6 6 12 12M18 6 6 18"/>', capture.cancel),
                ('confirmCapture', '确认', '<path d="m5 12 4 4L19 6"/>', capture.confirm)):
            color = '#FFFFFF' if name == 'confirmCapture' else COLORS['muted']
            button = QPushButton(tool_icon(glyph, color), '')
            button.setObjectName(name)
            button.setAccessibleName(label); button.setToolTip(label)
            button.setFixedSize(28,28); button.setIconSize(QSize(18,18))
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(action); row.addWidget(button)
            if name == 'resetCapture': self.reset_button = button
        self.toolbar.hide()

    def local_rect(self, native):
        rect = native.translated(-self.frame.pixels.topLeft())
        x = self.width()/self.frame.image.width()
        y = self.height()/self.frame.image.height()
        return QRectF(rect.x()*x, rect.y()*y, rect.width()*x, rect.height()*y)

    def native_point(self, event):
        point = event.position()
        if QRectF(self.rect()).contains(point):
            x = round(point.x()*self.frame.image.width()/self.width())
            y = round(point.y()*self.frame.image.height()/self.height())
            return self.frame.pixels.topLeft()+QPoint(x,y)
        # An implicit mouse grab can carry a drag onto another monitor.
        return desktop_pointer()

    def present_controls(self):
        saved = self.capture.selections
        visible = self.capture.owner is self and not self.capture.dragging and (self.capture.ready or bool(saved))
        self.toolbar.setVisible(visible)
        if visible:
            selected = self.capture.region if self.capture.ready else saved[-1]
            count = len(saved) + int(self.capture.ready)
            self.caption.setText(f'{count} 张 · {selected.width()} × {selected.height()}')
            reset_label = '重选' if self.capture.ready else '撤回上一张'
            self.reset_button.setAccessibleName(reset_label)
            self.reset_button.setToolTip(reset_label)
            self.toolbar.adjustSize()
            end = self.local_rect(selected.intersected(self.frame.pixels))
            x = round(end.right()) - self.toolbar.width()
            y = round(end.bottom()) + 9
            if y + self.toolbar.height() > self.height(): y = round(end.top()) - self.toolbar.height() - 9
            self.toolbar.move(max(8,min(x,self.width()-self.toolbar.width()-8)),
                              max(8,min(y,self.height()-self.toolbar.height()-8)))
            self.toolbar.raise_()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawImage(QRectF(self.rect()), self.frame.image, QRectF(self.frame.image.rect()))
        region = self.capture.region
        selections = [self.local_rect(rect) for rect in self.capture.selections]
        if not region.isEmpty(): selections.append(self.local_rect(region))
        shade = QPainterPath(); shade.addRect(QRectF(self.rect()))
        holes = QPainterPath(); holes.setFillRule(Qt.FillRule.WindingFill)
        for rect in selections: holes.addRect(rect)
        dim = QColor('#302B2D'); dim.setAlpha(154)
        painter.fillPath(shade.subtracted(holes), dim)
        if selections:
            painter.setPen(QPen(QColor('#FFFFFF'),3.5))
            for rect in selections: painter.drawRect(rect)
            painter.setPen(QPen(QColor(COLORS['primary']),1.5))
            for rect in selections: painter.drawRect(rect)
        elif self.capture.owner is self:
            box = QRectF((self.width()-306)/2, 28, 306, 38)
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(COLORS['surface']))
            painter.drawRoundedRect(box,10,10)
            painter.setPen(QColor(COLORS['text']))
            painter.drawText(box,Qt.AlignmentFlag.AlignCenter,'拖动框选截图 · Esc 取消')

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.capture.start = self.capture.end = self.native_point(event)
            self.capture.owner = self
            self.capture.dragging = True
            self.capture.repaint()
        elif event.button() == Qt.MouseButton.RightButton:
            self.capture.cancel()

    def mouseMoveEvent(self, event):
        if self.capture.dragging:
            self.capture.end = self.native_point(event)
            self.capture.repaint()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.capture.dragging:
            self.capture.end = self.native_point(event)
            self.capture.dragging = False
            point = self.capture.end
            self.capture.owner = next((overlay for overlay in self.capture.overlays if overlay.frame.pixels.contains(point)),self)
            self.capture.repaint()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape: self.capture.cancel()
        elif event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter): self.capture.confirm()
        elif event.key() == Qt.Key.Key_Space: self.capture.add_selection()
        else: super().keyPressEvent(event)


class DesktopCapture:
    def __init__(self, finished):
        self.finished = finished
        self.overlays = []
        self.start = self.end = None
        self.selections = []
        self.owner = None
        self.dragging = self.closed = False

    @property
    def region(self):
        return selection_rect(self.start,self.end) if self.start is not None else QRect()

    @property
    def ready(self):
        return self.region.width()>=3 and self.region.height()>=3

    def open(self):
        # Let the window compositor present the desktop after our panels hide.
        QTimer.singleShot(100,self.grab)

    def grab(self):
        if self.closed: return
        try:
            screens = []
            for screen in QGuiApplication.screens():
                image = screen.grabWindow(0).toImage()
                if image.isNull(): raise RuntimeError('无法读取显示器画面，请重新截图')
                image.setDevicePixelRatio(1.)
                screens.append((screen, ScreenFrame(screen.geometry(),image)))
            if not screens: raise RuntimeError('未检测到可截图的显示器')
            for screen, frame in screens: self.overlays.append(SelectionOverlay(self,screen,frame))
            point = desktop_pointer()
            self.owner = next((overlay for overlay in self.overlays if overlay.frame.pixels.contains(point)),self.overlays[0])
            for overlay in self.overlays: overlay.show(); overlay.raise_()
            self.owner.activateWindow(); self.owner.setFocus()
        except (RuntimeError, OSError) as exc:
            self.finish((),str(exc))

    def repaint(self):
        for overlay in self.overlays: overlay.present_controls()

    def reset(self):
        if not self.ready and self.selections:
            self.selections.pop()
        self.start = self.end = None
        self.dragging = False
        self.repaint()

    def add_selection(self):
        if not self.closed and not self.dragging and self.ready:
            self.selections.append(self.region)
            self.reset()

    def confirm(self):
        if self.closed or self.dragging or not (self.ready or self.selections): return
        try:
            frames = [overlay.frame for overlay in self.overlays]
            regions = [*self.selections, self.region] if self.ready else self.selections
            images = tuple(crop_selection(frames, region) for region in regions)
            if any(image.isNull() for image in images):
                raise RuntimeError('无法读取选区，请重新截图')
            self.finish(images)
        except RuntimeError as exc:
            self.finish((),str(exc))

    def cancel(self): self.finish(())

    def finish(self, images, error=None):
        if self.closed: return
        self.closed = True
        for overlay in self.overlays: overlay.hide(); overlay.deleteLater()
        self.overlays.clear()
        self.owner = None
        self.finished(images,error)
