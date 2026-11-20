"""Native desktop avatar and independent assistant windows."""

import math

from PIL import Image, ImageFilter
from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QLineF, QObject, QPoint, QRect, QRectF, QSize, QTimer, Qt, QVariantAnimation
from PySide6.QtGui import QBrush, QColor, QCursor, QGuiApplication, QIcon, QImage, QLinearGradient, QPainter, QPen, QTransform
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizeGrip, QVBoxLayout, QWidget
from client_fonts import emphasis_font
from client_hardware import HardwareReading, HardwareSampler
from client_scene import PHONE_BODY_RADIUS, pil_pixmap
from client_theme import COLORS, themed


WINDOW_FLAGS = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint


def available_at(point):
    screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
    return screen.availableGeometry()


def constrain(point, size, area):
    return QPoint(max(area.left(), min(point.x(), area.right() - size.width() + 1)),
                  max(area.top(), min(point.y(), area.bottom() - size.height() + 1)))


def show_window(window):
    window.showNormal()
    window.raise_()
    window.activateWindow()


class WindowDrag(QObject):
    def __init__(self, handle, target, move=None, *, started=None, finished=None):
        super().__init__(handle)
        self.target = target
        self.move = move or self.move_window
        self.started = started
        self.finished = finished
        self.press = None
        self.dragging = False
        handle.setCursor(Qt.CursorShape.OpenHandCursor)
        handle.installEventFilter(self)

    def move_window(self, position):
        area = available_at(QCursor.pos())
        self.target.move(constrain(position, self.target.size(), area))

    def eventFilter(self, handle, event):
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self.press = event.globalPosition().toPoint()
            self.origin = self.target.pos()
            self.dragging = False
        elif kind == QEvent.Type.MouseMove and self.press is not None:
            delta = event.globalPosition().toPoint() - self.press
            if self.dragging or delta.manhattanLength() > 5:
                if not self.dragging:
                    self.dragging = True
                    if self.started is not None:
                        self.started()
                self.move(self.origin + delta)
                return True
        elif kind in (QEvent.Type.MouseButtonRelease, QEvent.Type.UngrabMouse, QEvent.Type.Hide):
            if kind == QEvent.Type.MouseButtonRelease and event.button() != Qt.MouseButton.LeftButton:
                return False
            if kind == QEvent.Type.MouseButtonRelease and self.dragging and self.finished is not None:
                self.move(self.origin + event.globalPosition().toPoint() - self.press)
            self.press = None
            if self.dragging:
                self.dragging = False
                if self.finished is not None:
                    self.finished(kind == QEvent.Type.MouseButtonRelease)
                if isinstance(handle, QPushButton):
                    handle.setDown(False)
                return kind == QEvent.Type.MouseButtonRelease
        return False


def hardware_rows(reading):
    def value(number, unit):
        return '—' if number is None else f'{number:.0f}{unit}'
    rows=[('CPU', value(reading.cpu_load, '%'), ''),
          ('GPU', value(reading.gpu_load, '%'), value(reading.gpu_temperature, '°C')),
          ('内存', value(reading.memory_load, '%'), '')]
    if reading.game_fps is not None:
        rows.append(('游戏', value(reading.game_fps, ' FPS'), ''))
    return rows


class AvatarButton(QPushButton):
    def __init__(self, pixmap, parent):
        super().__init__(parent)
        self.pixmap = pixmap
        self.frosted = None
        self.sampler = None
        self.setAccessibleName('若娜瓦圆形头像')
        self.setAccessibleDescription('点击查看本机硬件状态')
        self.setMouseTracking(True)
        self.animation = QVariantAnimation(self, duration=320, startValue=0., endValue=1.)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.animation.valueChanged.connect(self.update_glass)
        self.animation.finished.connect(self.finish_animation)
        self.hold = QTimer(self, singleShot=True, interval=5000, timerType=Qt.TimerType.PreciseTimer)
        self.hold.timeout.connect(self.restore_portrait)
        self.refresh = QTimer(self, interval=250)
        self.refresh.timeout.connect(self.update)
        self.clicked.connect(self.show_hardware)

    def show_hardware(self):
        if self.frosted is None and not self.pixmap.isNull():
            image = self.pixmap.scaled(QSize(200, 200), Qt.AspectRatioMode.IgnoreAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
            rgba = image.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            pixels = Image.frombytes('RGBA', (rgba.width(), rgba.height()), bytes(rgba.constBits()))
            self.frosted = pil_pixmap(pixels.filter(ImageFilter.GaussianBlur(11)))
        if self.sampler is None:
            self.sampler = HardwareSampler()
            self.refresh.start()
        self.animation.setDirection(QAbstractAnimation.Direction.Forward)
        if self.animation.currentTime() == self.animation.duration():
            self.hold.start()
        else:
            self.animation.start()

    def update_glass(self, _value):
        self.update()

    def finish_animation(self):
        if self.animation.direction() == QAbstractAnimation.Direction.Forward:
            self.hold.start()
        else:
            self.stop_sampling()

    def restore_portrait(self):
        self.animation.setDirection(QAbstractAnimation.Direction.Backward)
        self.animation.start()

    def stop_sampling(self):
        self.hold.stop()
        self.refresh.stop()
        if self.sampler is not None:
            self.sampler.stop()
            self.sampler = None

    def hideEvent(self, event):
        self.animation.stop()
        self.animation.setCurrentTime(0)
        self.stop_sampling()
        super().hideEvent(event)

    def image_brush(self, pixmap):
        brush = QBrush(pixmap)
        size = pixmap.deviceIndependentSize()
        if not pixmap.isNull():
            brush.setTransform(QTransform.fromScale(self.width()/size.width(), self.height()/size.height()))
        return brush

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.image_brush(self.pixmap))
        painter.drawEllipse(rect)
        progress = self.animation.currentValue() or 0.
        if not progress:
            return
        painter.setOpacity(progress)
        if self.frosted is not None:
            painter.setBrush(self.image_brush(self.frosted))
            painter.drawEllipse(rect)
        tint = QLinearGradient(rect.topLeft(), rect.bottomRight())
        tint.setColorAt(0, QColor(255, 248, 252, 195))
        tint.setColorAt(1, QColor(242, 223, 235, 217))
        painter.setBrush(tint)
        painter.setPen(QPen(QColor(255, 255, 255, 230), 1.4))
        painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
        self.paint_readings(painter)

    def paint_readings(self, painter):
        reading = self.sampler.reading if self.sampler else HardwareReading()

        rows = hardware_rows(reading)
        center = self.width() / 2
        top = (self.height() - len(rows) * 22) / 2
        font = self.font()
        font.setPixelSize(13)
        painter.setFont(font)
        painter.setPen(QColor('#493445'))
        for index, (label, reading_text, temperature) in enumerate(rows):
            y = top + index * 22
            painter.drawText(QRectF(center-56, y, 36, 20), Qt.AlignmentFlag.AlignVCenter, label)
            painter.drawText(QRectF(center-20, y, 48, 20), Qt.AlignmentFlag.AlignCenter, reading_text)
            painter.drawText(QRectF(center+29, y, 31, 20), Qt.AlignmentFlag.AlignCenter, temperature)


class OrbitButton(QPushButton):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self.underMouse() or self.hasFocus()
        painter.setBrush(QColor('#FFE9F1' if active else '#F6F0F5'))
        painter.setPen(QPen(QColor(COLORS['primary'] if active else '#EADDE7'), 1))
        painter.drawEllipse(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5))
        rect = QRect(QPoint(), self.iconSize())
        rect.moveCenter(self.rect().center())
        self.icon().paint(painter, rect)


class DesktopLauncher(QWidget):
    SIZE = 284
    AVATAR = 164
    RADIUS = 112
    ANGLES = (-170, -132, -94)
    FOUR_ANGLES = (-174, -140, -106, -72)
    FIVE_ANGLES = (-182, -152, -122, -92, -62)

    def __init__(self, assets, icons, actions):
        super().__init__(None, WINDOW_FLAGS | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle('若娜瓦桌面助手')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.center = self.rect().center()
        self.avatar = AvatarButton(assets.avatar, self)
        self.avatar.setFixedSize(self.AVATAR, self.AVATAR)
        self.avatar.move(self.center - QPoint(self.AVATAR // 2, self.AVATAR // 2))
        self.avatar.installEventFilter(self)
        self.drag = WindowDrag(self.avatar, self, self.move_avatar)
        self.task_notice = QLabel('任务已完成', self)
        self.task_notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.task_notice.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.task_notice.setGeometry(48, 13, self.SIZE - 96, 42)
        self.task_notice.setStyleSheet('background:#eeeeee;color:#343034;'
                                       'border:1px solid #c9c4c8;border-radius:14px;'
                                       'padding:6px;font-size:14px;')
        self.task_notice.hide()
        self.task_notice_timer = QTimer(self, singleShot=True, interval=8000)
        self.task_notice_timer.timeout.connect(self.task_notice.hide)
        self.buttons = []
        self.keys = []
        for key, label, callback in actions:
            item = OrbitButton(QIcon(str(icons / (key + '.svg'))), '', self)
            item.setFixedSize(52, 52)
            item.setIconSize(QSize(30, 30))
            item.setAccessibleName(label)
            item.setToolTip(label)
            item.setCursor(Qt.CursorShape.PointingHandCursor)
            item.clicked.connect(lambda _checked=False, action=callback: self.activate(action))
            self.buttons.append(item)
            self.keys.append(key)
        self.expanded = False
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(600)
        self.animation.valueChanged.connect(self.place_buttons)
        self.enter_timer = QTimer(self, singleShot=True, interval=150)
        self.enter_timer.timeout.connect(lambda: self.expand(True))
        self.leave_timer = QTimer(self, singleShot=True, interval=300)
        self.leave_timer.timeout.connect(lambda: self.expand(False))
        self.watch = QTimer(self, interval=40)
        self.watch.timeout.connect(self.watch_pointer)
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.move(area.bottomRight() - self.center - QPoint(105, 105))
        self.place_buttons(0.)

    def activate(self, action):
        action()

    def show_task_completed(self, _title=''):
        self.task_notice.show()
        self.task_notice.raise_()
        self.task_notice_timer.start()

    def set_pinned(self, pinned):
        flag = Qt.WindowType.WindowStaysOnTopHint
        if bool(self.windowFlags() & flag) != pinned:
            visible = self.isVisible()
            position = self.pos()
            self.setWindowFlag(flag, pinned)
            self.move(position)
            if visible:
                self.show()

    def move_avatar(self, position):
        area = available_at(position + self.center)
        avatar = self.avatar.geometry()
        self.move(constrain(position + avatar.topLeft(), avatar.size(), area) - avatar.topLeft())
        self.expand(False)

    def eventFilter(self, watched, event):
        if watched is self.avatar:
            if (event.type() in (QEvent.Type.Enter, QEvent.Type.MouseMove) and
                    not self.expanded and not self.enter_timer.isActive() and not self.avatar.isDown()):
                self.enter_timer.start()
            elif event.type() in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress):
                self.enter_timer.stop()
            elif (event.type() == QEvent.Type.MouseButtonRelease and self.avatar.underMouse()
                  and not self.expanded):
                self.enter_timer.start()
        return super().eventFilter(watched, event)

    def expand(self, value):
        self.enter_timer.stop()
        if value == self.expanded:
            return
        self.expanded = value
        self.leave_timer.stop()
        self.animation.stop()
        self.animation.setStartValue(self.animation.currentValue() or 0.)
        self.animation.setEndValue(600. if value else 0.)
        self.animation.setDuration(600 if value else 240)
        self.animation.start()
        if value:
            self.watch.start()
        else:
            self.watch.stop()

    def watch_pointer(self):
        point = self.mapFromGlobal(QCursor.pos())
        delta = point - self.center
        keyboard = any(button.hasFocus() for button in self.buttons)
        if keyboard or delta.x() ** 2 + delta.y() ** 2 <= (self.RADIUS + 33) ** 2:
            self.leave_timer.stop()
        elif not self.leave_timer.isActive():
            self.leave_timer.start()

    def place_buttons(self, elapsed):
        travel = 600 - (len(self.buttons) - 1) * 70
        for index, button in enumerate(self.buttons):
            progress = max(0., min(1., (elapsed - index * 70) / travel))
            if progress == 0.:
                button.hide()
                continue
            eased = QEasingCurve(QEasingCurve.Type.OutCubic).valueForProgress(progress)
            button.move(self.orbit_position(index, eased))
            button.show()

    def orbit_position(self, index, progress=1.):
        global_center = self.mapToGlobal(self.center)
        area = available_at(global_center)
        flip = global_center.x() - area.left() < self.SIZE // 2
        down = global_center.y() - area.top() < self.SIZE // 2
        angles = {4: self.FOUR_ANGLES, 5: self.FIVE_ANGLES}.get(len(self.buttons), self.ANGLES)
        angle = -60 + (angles[index] + 60) * progress
        angle = math.radians(180 - angle if flip else angle)
        offset = QPoint(round(self.RADIUS * math.cos(angle)),
                        round(self.RADIUS * math.sin(angle) * (-1 if down else 1)))
        return self.center + offset - QPoint(26, 26)

    def icon_rect(self, key, *, moving=False):
        index = self.keys.index(key)
        button = self.buttons[index]
        origin = button.pos() if moving and button.isVisible() else self.orbit_position(index)
        return QRect(self.mapToGlobal(origin), button.size())

    def hideEvent(self, event):
        self.expand(False)
        super().hideEvent(event)


class CloseButton(QPushButton):
    def __init__(self, parent=None, *, color=None):
        super().__init__(parent)
        self.glyph_color = QColor(color) if color is not None else None
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = min(self.width(), self.height())
        reach = size * .15
        painter.translate(QRectF(self.rect()).center())
        painter.setPen(QPen(self.glyph_color or QColor(COLORS['muted']), max(1.4, size * .06), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QLineF(-reach, -reach, reach, reach))
        painter.drawLine(QLineF(-reach, reach, reach, -reach))


class FloatingPanel(QWidget):
    def __init__(self, title, content, size, close_panel, *, resizable=False, inner_corner_radius=None):
        super().__init__(None, WINDOW_FLAGS)
        self.setWindowTitle(title)
        self.setObjectName('assistantPanel')
        self.close_panel = close_panel
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(themed('''
            QLabel#assistantTitle{color:$text;font-size:18px;}
            QPushButton#assistantClose{background:$elevated;border:0;border-radius:14px;}
            QPushButton#assistantClose:hover, QPushButton#assistantClose:focus{background:$selection;}'''))
        layout = QVBoxLayout(self)
        inset = 22
        self.corner_radius = PHONE_BODY_RADIUS if inner_corner_radius is None else inset + inner_corner_radius
        layout.setContentsMargins(inset, 13, inset, 19 if inner_corner_radius is None else inset)
        header = QHBoxLayout()
        name = self.title_label = QLabel(title)
        name.setFont(emphasis_font())
        name.setObjectName('assistantTitle')
        name.setFixedHeight(36)
        self.drag = WindowDrag(name, self)
        header.addWidget(name, 1)
        self.dismiss = CloseButton()
        self.dismiss.setObjectName('assistantClose')
        self.dismiss.setAccessibleName('关闭' + title)
        self.dismiss.setFixedSize(28, 28)
        self.dismiss.clicked.connect(self.close)
        header.addWidget(self.dismiss)
        layout.addLayout(header)
        layout.addWidget(content, 1)
        self.grip = QSizeGrip(self) if resizable else None
        if self.grip is not None:
            self.grip.setToolTip('拖动调整工作区大小')
            self.grip.setFixedSize(22,22)
            self.setMouseTracking(True)
        self.resize(*size)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.grip is not None:
            self.grip.move(self.width()-self.grip.width()-34, self.height()-self.grip.height()-6)
            self.grip.raise_()

    def resize_edges(self, position):
        edges = Qt.Edge(0)
        if self.grip is None:
            return edges
        if position.x() < 8:
            edges |= Qt.Edge.LeftEdge
        elif position.x() >= self.width()-8:
            edges |= Qt.Edge.RightEdge
        if position.y() < 8:
            edges |= Qt.Edge.TopEdge
        elif position.y() >= self.height()-8:
            edges |= Qt.Edge.BottomEdge
        return edges

    def mouseMoveEvent(self, event):
        edges = self.resize_edges(event.position())
        if edges in (Qt.Edge.LeftEdge|Qt.Edge.TopEdge, Qt.Edge.RightEdge|Qt.Edge.BottomEdge):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif edges in (Qt.Edge.LeftEdge|Qt.Edge.BottomEdge, Qt.Edge.RightEdge|Qt.Edge.TopEdge):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif edges & (Qt.Edge.LeftEdge|Qt.Edge.RightEdge):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif edges:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        edges = self.resize_edges(event.position())
        handle = self.windowHandle()
        if edges and event.button() == Qt.MouseButton.LeftButton and handle and handle.startSystemResize(edges):
            event.accept()
            return
        super().mousePressEvent(event)

    def place_near(self, anchor):
        if not self.testAttribute(Qt.WidgetAttribute.WA_Moved):
            area = available_at(anchor)
            self.resize(min(self.width(), area.width()), min(self.height(), area.height()))
            self.move(constrain(anchor - QPoint(self.width() + 120, self.height() // 2), self.size(), area))

    def panel_rect(self):
        return self.rect()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QLinearGradient(0,0,self.width(),self.height())
        fill.setColorAt(0,QColor(COLORS['surface']))
        fill.setColorAt(1,QColor(COLORS['background']))
        painter.setBrush(fill)
        painter.setPen(QPen(QColor(COLORS['gray']),1))
        rect = QRectF(self.panel_rect()).adjusted(.5,.5,-.5,-.5)
        radius = self.corner_radius-.5
        painter.drawRoundedRect(rect,radius,radius)

    def closeEvent(self, event):
        event.ignore()
        self.close_panel(self)


class PanelMotion(QWidget):
    HANDOFF_MS = 32

    def __init__(self):
        super().__init__(None, WINDOW_FLAGS | Qt.WindowType.WindowStaysOnTopHint |
                         Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.frame = None
        self.animation = QVariantAnimation(self)
        self.animation.setStartValue(0.)
        self.animation.setEndValue(1.)
        self.animation.valueChanged.connect(self.step)
        self.animation.finished.connect(self.finish)
        self.handoff_window = None
        self.handoff_timer = QTimer(self, singleShot=True, timerType=Qt.TimerType.PreciseTimer)
        self.handoff_timer.timeout.connect(self.complete)

    def play(self, frame, source, destination, opening, done, *, opening_target=None):
        self.frame = frame
        self.source, self.destination = source, destination
        self.opening, self.done = opening, done
        self.opening_target = opening_target
        self.fixed_canvas = QGuiApplication.screenAt(source.center()) == QGuiApplication.screenAt(destination.center())
        offset = QPoint()
        if self.fixed_canvas:
            canvas = source.united(destination)
            offset = -canvas.topLeft()
            self.setGeometry(canvas)
        self.start_rect = QRectF(source.translated(offset))
        self.end_rect = QRectF(destination.translated(offset))
        self.set_reversed(False)
        self.animation.setDuration(320 if opening else 220)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.step(0.)
        self.show()
        self.raise_()
        self.animation.start()

    def set_reversed(self, reverse):
        direction = QVariantAnimation.Direction
        self.animation.setDirection(direction.Backward if reverse else direction.Forward)

    def step(self, progress):
        if self.frame is None: return
        start, end = self.start_rect, self.end_rect
        self.draw_rect = QRectF(start.topLeft() + (end.topLeft()-start.topLeft())*progress,
                                start.size() + (end.size()-start.size())*progress)
        if not self.fixed_canvas:
            # A large window spanning screens may use different scaling at
            # each end. Let its native position follow the moving snapshot.
            self.setGeometry(self.draw_rect.toRect())
            self.draw_rect = QRectF(self.rect())
        self.update()

    def paintEvent(self, event):
        if self.frame is None: return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        progress = self.animation.currentValue()
        painter.setOpacity(.18 + .82 * (progress if self.opening else 1. - progress))
        painter.drawPixmap(self.draw_rect, self.frame, QRectF(self.frame.rect()))

    def finish(self):
        target = self.opening_target() if self.opening_target else None
        if target is None:
            self.complete()
            return
        window, reveal = target
        self.handoff_window = window
        window.installEventFilter(self)
        # An occluded window may never receive a paint event.
        self.handoff_timer.start(250)
        reveal()

    def eventFilter(self, watched, event):
        if watched is self.handoff_window:
            if event.type() == QEvent.Type.Paint and self.handoff_timer.interval() != self.HANDOFF_MS:
                # Keep the snapshot up while Qt flushes the first paint and
                # the desktop compositor presents the real window underneath.
                self.handoff_timer.start(self.HANDOFF_MS)
            elif event.type() == QEvent.Type.Hide:
                self.handoff_timer.start(0)
        return super().eventFilter(watched, event)

    def clear(self):
        self.handoff_timer.stop()
        if self.handoff_window is not None:
            self.handoff_window.removeEventFilter(self)
            self.handoff_window = None
        self.hide()
        self.frame = None

    def complete(self):
        self.clear()
        self.done()

    def stop(self):
        self.animation.stop()
        self.clear()


class PanelTransitions:
    """Run the closing animation first, then open the last requested panel."""

    def __init__(self, launcher):
        self.launcher = launcher
        self.motion = PanelMotion()
        self.current = self.requested = None
        self.prepared = None

    def open(self, key, window, dismiss, reveal):
        self.requested = (key, window, dismiss, reveal, self.launcher.icon_rect(key, moving=True))
        self.prepared = None
        if (self.current and self.current[1] is window and self.motion.frame is not None and
                self.motion.opening and not window.isVisible()):
            self.motion.set_reversed(False)
        self.advance()

    def toggle(self, key, open_panel):
        if self.requested and self.requested[0] == key:
            self.close(self.requested[1])
        else:
            open_panel()

    def close(self, window):
        if self.requested and self.requested[1] is window:
            self.requested = None
            self.prepared = None
            if (self.current and self.current[1] is window and self.motion.frame is not None and
                    self.motion.opening and not window.isVisible()):
                self.motion.set_reversed(True)
            self.advance()

    def advance(self):
        if self.motion.frame is not None:
            return
        if self.current:
            key, window, dismiss, reveal, _ = self.current
            if self.requested and self.requested[1] is window:
                if window.isVisible(): reveal()
                return
            frame, source = window.grab(), window.geometry()
            self.current = None
            dismiss()
            if self.requested:
                self.prepared = (self.requested, self.requested[1].grab())
            self.motion.play(frame, source, self.launcher.icon_rect(key, moving=True), False, self.advance)
        elif self.requested:
            self.current = self.requested
            _, window, dismiss, reveal, anchor = self.current
            def opening_target():
                if self.requested and self.requested[1] is window:
                    return window, reveal

            def opened():
                if opening_target() is not None:
                    return
                if self.motion.animation.direction() == QVariantAnimation.Direction.Backward:
                    self.current = None
                    dismiss()
                self.advance()

            frame = self.prepared[1] if self.prepared and self.prepared[0] is self.current else window.grab()
            self.prepared = None
            self.motion.play(frame, anchor, window.geometry(), True, opened, opening_target=opening_target)

    def stop(self):
        self.current = self.requested = None
        self.prepared = None
        self.motion.stop()
