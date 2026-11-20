"""The floating workspace and its right-edge dock."""
from PySide6.QtCore import QEasingCurve, QPoint, QRect, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QLayout, QPushButton, QWidget

from client_shell import WINDOW_FLAGS, FloatingPanel, constrain
from client_theme import COLORS


class DockOverlay(QWidget):
    """Paint the drag outline or a frozen workspace without taking mouse input."""

    def __init__(self, parent):
        super().__init__(parent, WINDOW_FLAGS | Qt.WindowType.WindowStaysOnTopHint |
                         Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.frame = None
        self.final_frame = None
        self.frame_rect = QRectF()
        self.blend = 0.
        self.outline_radius = 11

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.frame is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.setOpacity(1.-self.blend)
            painter.drawPixmap(self.frame_rect,self.frame,QRectF(self.frame.rect()))
            if self.final_frame is not None:
                painter.setOpacity(self.blend)
                painter.drawPixmap(self.frame_rect,self.final_frame,QRectF(self.final_frame.rect()))
            return
        outline = QColor(COLORS['primary'])
        tint = QColor(outline)
        tint.setAlpha(36)
        painter.setBrush(tint)
        painter.setPen(QPen(outline,2))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(2,2,-2,-2),self.outline_radius,self.outline_radius)


class WorkspaceWindow(FloatingPanel):
    SNAP_DISTANCE = 24
    HANDLE_WIDTH = 14
    HANDLE_HEIGHT = 76
    HANDLE_GAP = 3

    def __init__(self, pane, close_panel):
        super().__init__('若娜瓦加班区', pane, (920,700), close_panel, resizable=True,
                         inner_corner_radius=pane.INNER_CORNER_RADIUS)
        self.pane = pane
        self.docked_screen = None
        self.snap_screen = None
        self.floating_target = None
        self.floating_size = self.size()
        self.collapsed = False
        self.floating_radius = self.corner_radius
        self.floating_margins = self.layout().contentsMargins()
        self.drag.move = self.move_workspace
        self.drag.finished = self.finish_drag

        self.fold = QPushButton('›', self)
        self.fold.setObjectName('workDockHandle')
        self.fold.setFixedSize(self.HANDLE_WIDTH,self.HANDLE_HEIGHT)
        self.fold.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fold.setStyleSheet('''
            QPushButton#workDockHandle { background:#232226; color:#fff; border:0;
                padding:0; border-top-left-radius:6px; border-bottom-left-radius:6px; }
            QPushButton#workDockHandle:hover, QPushButton#workDockHandle:focus { background:#45434A; }''')
        self.fold.clicked.connect(lambda: self.set_collapsed(not self.collapsed,animated=True))
        self.fold.hide()
        self.dock_overlay = DockOverlay(self)
        self.dock_motion = QVariantAnimation(self)
        self.dock_motion.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.dock_motion.valueChanged.connect(self.draw_motion)
        self.dock_motion.finished.connect(self.stop_motion)
        QGuiApplication.instance().screenRemoved.connect(self.screen_removed)

    def move_workspace(self, position):
        if self.dock_overlay.frame is not None:
            return
        delta = position - self.drag.origin
        pointer = self.drag.press + delta
        screen = QGuiApplication.screenAt(pointer) or self.docked_screen or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        if self.docked_screen is not None:
            self.floating_target = None
            if delta.x() <= -2*self.SNAP_DISTANCE:
                size = self.floating_size.boundedTo(area.size())
                origin = pointer - QPoint(min(100,size.width()//3),18)
                self.floating_target = QRect(constrain(origin,size,area),size)
            self.show_outline(self.floating_target,self.floating_radius)
            return
        right_gap = area.right() + 1 - (position.x() + self.width())
        outline = None
        if 0 <= area.right() - pointer.x() and right_gap <= self.SNAP_DISTANCE:
            self.snap_screen = screen
            outline = self.docked_geometry(screen)
            outline.adjust(self.HANDLE_WIDTH+self.HANDLE_GAP,0,0,0)
        else:
            self.snap_screen = None
        self.move(constrain(position,self.size(),area))
        self.show_outline(outline,11)

    def show_outline(self, geometry, radius):
        if geometry is None:
            self.dock_overlay.hide()
            return
        self.dock_overlay.outline_radius = radius
        self.dock_overlay.setGeometry(geometry)
        self.dock_overlay.update()
        if self.dock_overlay.isHidden():
            self.dock_overlay.show()
            self.dock_overlay.raise_()

    def finish_drag(self, released):
        screen = self.snap_screen if released else None
        target = self.floating_target if released else None
        self.snap_screen = self.floating_target = None
        if self.dock_overlay.frame is None:
            self.dock_overlay.hide()
        if screen is not None:
            self.dock(screen,animated=True)
        elif target is not None:
            self.undock(target,animated=True)

    @staticmethod
    def docked_geometry(screen):
        area = screen.availableGeometry()
        width = max(1,area.width() // 4)
        return QRect(area.right()-width+1,area.top(),width,area.height())

    def dock(self, screen, *, animated=False):
        if self.docked_screen is not None:
            return
        source = self.geometry()
        frame = self.grab() if animated else None
        if frame is not None:
            self.setWindowOpacity(0.)
        self.floating_size = self.size()
        self.docked_screen = screen
        self.corner_radius = 12
        self.title_label.setText('工作区')
        self.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.setMinimumSize(0,0)
        self.grip.hide()
        self.pane.set_docked(True)
        screen.availableGeometryChanged.connect(self.dock_area_changed)
        self.set_collapsed(False)
        if frame is not None:
            self.animate_layout(frame,source)

    def undock(self, target=None, *, animated=False):
        self.stop_motion()
        source = self.geometry()
        frame = self.grab() if animated and self.isVisible() else None
        if frame is not None:
            self.setWindowOpacity(0.)
        self.docked_screen.availableGeometryChanged.disconnect(self.dock_area_changed)
        self.set_collapsed(False)
        self.docked_screen = None
        self.fold.hide()
        self.corner_radius = self.floating_radius
        self.title_label.setText(self.windowTitle())
        self.pane.set_docked(False)
        self.layout().setContentsMargins(self.floating_margins)
        self.layout().setSizeConstraint(QLayout.SizeConstraint.SetDefaultConstraint)
        if target is None:
            self.resize(self.floating_size.boundedTo(self.screen().availableGeometry().size()))
        else:
            self.setGeometry(target)
        self.grip.show()
        self.update()
        if frame is not None:
            self.animate_layout(frame,source)

    def animate_layout(self, frame, source):
        destination = self.geometry()
        self.animate_dock(frame,source.united(destination),source,destination,280,final_frame=self.grab())

    def set_collapsed(self, collapsed, *, animated=False):
        if self.docked_screen is None:
            return
        if not animated and self.dock_overlay.frame is not None:
            self.stop_motion()
        if animated and not self.isVisible():
            animated = False
        if animated:
            self.stop_motion()
            area = self.docked_geometry(self.docked_screen)
            shifted = area.translated(area.width()-self.HANDLE_WIDTH,0)
            frame = self.grab() if collapsed else None
            self.setWindowOpacity(0.)
        self.collapsed = collapsed
        self.setUpdatesEnabled(False)
        margins = (0,0,0,0) if collapsed else (self.HANDLE_WIDTH+self.HANDLE_GAP+6,5,8,8)
        self.layout().setContentsMargins(*margins)
        for widget in (self.title_label,self.dismiss,self.pane):
            widget.setVisible(not collapsed)
        label = '展开工作区' if collapsed else '折叠工作区'
        self.fold.setText('‹' if collapsed else '›')
        self.fold.setAccessibleName(label)
        self.fold.setToolTip(label)
        self.fold.show()
        self.place_docked()
        self.setUpdatesEnabled(True)
        if animated:
            if collapsed:
                self.animate_dock(frame,area,area,shifted,250)
            else:
                self.animate_dock(self.grab(),area,shifted,area,260)

    def animate_dock(self, frame, canvas, source, destination, duration, *, final_frame=None):
        origin = -canvas.topLeft()
        self.dock_overlay.frame = frame
        self.dock_overlay.final_frame = final_frame
        self.dock_overlay.setGeometry(canvas)
        start = QRectF(source.translated(origin))
        self.dock_motion.setCurrentTime(0)
        self.dock_motion.setStartValue(start)
        self.dock_motion.setEndValue(QRectF(destination.translated(origin)))
        self.dock_motion.setDuration(duration)
        self.draw_motion(start)
        self.dock_overlay.show()
        self.dock_overlay.raise_()
        self.dock_motion.start()

    def draw_motion(self, rect):
        self.dock_overlay.frame_rect = rect
        self.dock_overlay.blend = (max(0.,min(1.,2*self.dock_motion.currentTime()/self.dock_motion.duration()-1.))
                                   if self.dock_overlay.final_frame is not None else 0.)
        self.dock_overlay.update()

    def stop_motion(self):
        if self.dock_overlay.frame is not None:
            self.dock_motion.stop()
            self.setWindowOpacity(1.)
            self.dock_overlay.frame = None
            self.dock_overlay.final_frame = None
        self.dock_overlay.hide()

    def dock_area_changed(self):
        self.stop_motion()
        self.place_docked()

    def place_docked(self):
        area = self.docked_screen.availableGeometry()
        if self.collapsed:
            geometry = QRect(QPoint(area.right()-self.HANDLE_WIDTH+1,area.center().y()-self.HANDLE_HEIGHT//2),
                             QSize(self.HANDLE_WIDTH,self.HANDLE_HEIGHT))
        else:
            geometry = self.docked_geometry(self.docked_screen)
            self.pane.arrange_docked_controls(geometry.width() < 300)
        self.setGeometry(geometry)
        self.position_handle()

    def screen_removed(self, screen):
        if screen is self.snap_screen or self.floating_target is not None:
            self.snap_screen = self.floating_target = None
            self.dock_overlay.hide()
        if screen is self.docked_screen:
            self.undock()
            if screen := QGuiApplication.primaryScreen():
                self.move(constrain(self.pos(),self.size(),screen.availableGeometry()))

    def hideEvent(self, event):
        self.snap_screen = self.floating_target = None
        self.stop_motion()
        super().hideEvent(event)

    def position_handle(self):
        self.fold.move(0,(self.height()-self.HANDLE_HEIGHT)//2)
        self.fold.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'fold'):
            self.position_handle()

    def resize_edges(self, position):
        return Qt.Edge(0) if self.docked_screen is not None else super().resize_edges(position)

    def panel_rect(self):
        rect = super().panel_rect()
        if self.docked_screen is not None:
            rect.adjust(self.HANDLE_WIDTH+self.HANDLE_GAP,0,0,0)
        return rect

    def paintEvent(self, event):
        if not self.collapsed:
            super().paintEvent(event)
