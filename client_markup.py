"""Image-space annotations shared by the editor, thumbnail, and sent image."""
from dataclasses import dataclass, replace
import math

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QConicalGradient, QFont, QFontMetricsF, QIcon, QKeySequence, QPainter, QPainterPath,
                           QPainterPathStroker, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QColorDialog, QFrame, QGridLayout, QInputDialog, QLabel,
                               QMenu, QPushButton, QScrollArea, QVBoxLayout, QWidget, QWidgetAction)

from client_fonts import emphasis_font
from client_shell import WindowDrag, constrain
from client_theme import COLORS, themed
from client_workspace_controls import tool_icon


DEFAULT_COLOR = COLORS['primary']
SHAPES = {'line': '直线', 'rect': '矩形', 'ellipse': '椭圆'}
PALETTE = (('白色','#FFFFFF'), ('灰色',COLORS['gray']), ('棕色','#92400E'), ('暗红色',DEFAULT_COLOR),
           ('橙色','#F97316'), ('琥珀色','#F59E0B'), ('绿色','#16A34A'), ('青绿色','#0D9488'),
           ('青色','#06B6D4'), ('蓝色','#2563EB'), ('靛蓝色','#4F46E5'), ('紫色','#9333EA'), ('玫红色','#DB2777'))


@dataclass(frozen=True)
class Mark:
    tool: str
    points: tuple
    color: str = DEFAULT_COLOR
    text: str = ''


def text_position(image, mark, point):
    font = QFont()
    font.setPixelSize(max(16, round(min(image.width(), image.height()) * .038)))
    metrics = QFontMetricsF(font)
    x = max(0., min(point[0], image.width() - metrics.horizontalAdvance(mark.text)))
    y = max(0., min(point[1], image.height() - metrics.height()))
    return font, metrics, (x, y)


def mark_path(image, mark):
    points = [QPointF(*point) for point in mark.points]
    start, end = points[0], points[-1]
    path = QPainterPath()
    if mark.tool == 'text':
        font, metrics, (x, y) = text_position(image, mark, mark.points[0])
        path.addText(QPointF(x, y + metrics.ascent()), font, mark.text)
    elif start == end and len(points) <= 2:
        path.moveTo(start)
        path.lineTo(start + QPointF(.01, 0))
    elif mark.tool == 'rect':
        path.addRect(QRectF(start, end).normalized())
    elif mark.tool == 'ellipse':
        path.addEllipse(QRectF(start, end).normalized())
    else:
        path.moveTo(start)
        for point in points[1:]:
            path.lineTo(point)
    return path


def stroke_width(image):
    return max(2., min(image.width(), image.height()) * .0028)


def swept_area(start, end, radius):
    path = QPainterPath()
    if start == end:
        path.addEllipse(QPointF(*start), radius, radius)
        return path
    path.moveTo(QPointF(*start)); path.lineTo(QPointF(*end))
    stroker = QPainterPathStroker()
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroker.setWidth(radius * 2)
    return stroker.createStroke(path)


def remaining_pen(mark, area, step):
    fragments = []
    points = []
    removed = False

    def retain(point):
        nonlocal removed
        if not area.contains(QPointF(*point)):
            points.append(point)
        else:
            removed = True
            if points:
                fragments.append(replace(mark, points=tuple(points)))
                points.clear()

    retain(mark.points[0])
    for start, end in zip(mark.points, mark.points[1:]):
        count = max(1, math.ceil(math.dist(start, end) / step))
        for index in range(1, count + 1):
            retain(tuple(a + (b - a) * index / count for a, b in zip(start, end)))
    if not removed:
        return [mark]
    if points:
        fragments.append(replace(mark, points=tuple(points)))
    return fragments


def paint_marks(painter, image, marks):
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    width = stroke_width(image)
    for mark in marks:
        path = mark_path(image, mark)
        outline = QPen(QColor('#FAFAFD'), width * 1.7, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.strokePath(path, outline)
        if mark.tool == 'text':
            painter.fillPath(path, QColor(mark.color))
        else:
            outline.setColor(QColor(mark.color))
            outline.setWidthF(width)
            painter.strokePath(path, outline)


def marked_image(image, marks):
    result = image.copy()
    if marks:
        painter = QPainter(result)
        paint_marks(painter,result,marks)
        painter.end()
    return result


class MarkupCanvas(QWidget):
    changed = Signal()
    text_requested = Signal(object)

    def __init__(self):
        super().__init__()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.image = None
        self.marks = ()
        self.undo_stack = []
        self.redo_stack = []
        self.stroke = []
        self.gesture_marks = None
        self.tool = 'pen'
        self.color = DEFAULT_COLOR
        self.zoom = 1.

    def load(self, image, marks):
        self.image = image
        self.marks = marks
        self.stroke = []
        self.gesture_marks = None
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.changed.emit()
        self.update()

    def scale_to(self, zoom):
        if self.image is None: return
        self.zoom = zoom
        self.resize(math.ceil(self.image.width()*zoom),math.ceil(self.image.height()*zoom))
        self.update()

    def point(self, event):
        pos = event.position()/self.zoom
        return (max(0.,min(self.image.width()-1.,pos.x())), max(0.,min(self.image.height()-1.,pos.y())))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS['background']))
        if self.image is None: return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, self.image.width()*self.zoom, self.image.height()*self.zoom), 8, 8)
        painter.setClipPath(clip)
        painter.scale(self.zoom,self.zoom)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(0,0,self.image)
        marks = (*self.marks,Mark(self.tool,tuple(self.stroke),self.color)) if self.stroke else self.marks
        paint_marks(painter,self.image,marks)

    def mousePressEvent(self, event):
        if self.image is None or event.button() != Qt.MouseButton.LeftButton:
            return
        point = self.point(event)
        if self.tool == 'pen' or self.tool in SHAPES:
            self.stroke = [point]
            self.update()
        elif self.tool == 'erase':
            self.begin_gesture(point)
            self.erase_to(point)
        elif self.tool in ('pointer', 'text'):
            margin = 3 / self.zoom
            for index in range(len(self.marks) - 1, -1, -1):
                mark = self.marks[index]
                if mark.tool == 'text' and mark_path(self.image, mark).boundingRect().adjusted(
                        -margin, -margin, margin, margin).contains(QPointF(*point)):
                    self.begin_gesture(point, index)
                    break

    def begin_gesture(self, point, index=None):
        self.gesture_marks = self.marks
        self.gesture_point = point
        self.gesture_index = index

    def mouseMoveEvent(self, event):
        if self.gesture_marks is not None:
            point = self.point(event)
            if self.gesture_index is None:
                self.erase_to(point)
            else:
                self.move_text(point)
        elif self.stroke:
            point = self.point(event)
            if self.tool=='pen':
                if point!=self.stroke[-1]: self.stroke.append(point)
            else:
                self.stroke[1:] = [point]
            self.update()

    def mouseReleaseEvent(self, event):
        if self.image is None or event.button() != Qt.MouseButton.LeftButton:
            return
        if self.gesture_marks is not None:
            self.mouseMoveEvent(event)
            original, self.gesture_marks = self.gesture_marks, None
            if self.marks != original:
                self.remember(original)
        elif self.stroke:
            self.mouseMoveEvent(event)
            self.commit((*self.marks,Mark(self.tool,tuple(self.stroke),self.color)))
        elif self.tool == 'text':
            self.text_requested.emit(self.point(event))

    def erase_to(self, point):
        width = stroke_width(self.image)
        radius = max(8 / self.zoom, width)
        area = swept_area(self.gesture_point, point, radius)
        pen_area = swept_area(self.gesture_point, point, radius + width * .85)
        self.gesture_point = point
        stroker = QPainterPathStroker()
        stroker.setWidth(width * 1.7)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        remaining = []
        for mark in self.marks:
            path = mark_path(self.image, mark)
            hit = area.intersects(stroker.createStroke(path)) or (mark.tool == 'text' and area.intersects(path))
            if not hit:
                remaining.append(mark)
            elif mark.tool == 'pen':
                remaining.extend(remaining_pen(mark, pen_area, max(.5, radius / 4)))
        self.marks = tuple(remaining)
        self.update()

    def move_text(self, point):
        index = self.gesture_index
        mark = self.gesture_marks[index]
        _, _, origin = text_position(self.image, mark, mark.points[0])
        offset = tuple(end - start for start, end in zip(self.gesture_point, point))
        target = tuple(value + delta for value, delta in zip(origin, offset))
        _, _, position = text_position(self.image, mark, target)
        moved = replace(mark, points=(position,)) if position != origin else mark
        self.marks = (*self.gesture_marks[:index], moved, *self.gesture_marks[index + 1:])
        self.update()

    def commit(self, marks):
        original = self.marks
        self.marks = marks
        self.stroke = []
        self.remember(original)

    def remember(self, original):
        self.undo_stack.append(original)
        self.redo_stack.clear()
        self.changed.emit()
        self.update()

    def undo(self):
        if self.gesture_marks is None and self.undo_stack:
            self.redo_stack.append(self.marks)
            self.marks = self.undo_stack.pop()
            self.changed.emit()
            self.update()

    def redo(self):
        if self.gesture_marks is None and self.redo_stack:
            self.undo_stack.append(self.marks)
            self.marks = self.redo_stack.pop()
            self.changed.emit()
            self.update()


TOOL_GLYPHS = {
    'grip': '<path d="M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01" stroke-width="2.8"/>',
    'pointer': '<path d="m5 4 14 6-6 3-3 6z"/>',
    'pen': '<path d="M4 15c3-4 8-10 10-10 4 0-3 8-5 12-1 3 2 1 5-2 4-4 6-2 4 1-2 4 0 4 3 2"/>',
    'text': '<path d="M6 7V5h12v2M12 5v14m-3 0h6"/>',
    'shape': '<circle cx="9" cy="9" r="5"/><rect x="10" y="10" width="10" height="10" rx="2"/>',
    'erase': '<path d="M5 18 3 16a2 2 0 0 1 0-3l9-9a2 2 0 0 1 3 0l5 5a2 2 0 0 1 0 3l-6 6a3 3 0 0 1-2 1H7a3 3 0 0 1-2-1ZM7 9l9 9"/>',
    'undo': '<path d="m8 4-5 5 5 5M3 9h10a6 6 0 0 1 0 12h-3"/>',
    'redo': '<path d="m16 4 5 5-5 5m5-5H11a6 6 0 0 0 0 12h3"/>',
    'close': '<path d="m5 5 14 14M19 5 5 19"/>',
    'done': '<path d="m5 12 4 4L19 6"/>',
}


class MarkupEditor(QWidget):
    accepted = Signal(object)
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName('markupEditor')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(themed('''
            QWidget#markupEditor { background:$surface; border-radius:15px; }
            QLabel#markupTitle { color:$text; }
            QFrame#markupTools { background:$surface; border:1px solid $border; border-radius:10px; }
            QFrame#markupTools QPushButton { background:transparent; border:0; border-radius:7px; padding:0; }
            QFrame#markupTools QPushButton:hover, QFrame#markupTools QPushButton:checked { background:$selection; }
            QFrame#markupTools QPushButton:focus { border:1px solid $gray; }
            QFrame#markupTools QPushButton::menu-indicator { image:none; width:0; }
            QFrame#markupTools QPushButton#markupDone { background:$primary; }
            QFrame#markupTools QPushButton#markupDone:hover { background:$primaryHover; }
            QWidget#markupDivider { background:$border; }
            QScrollArea { background:$background; border:0; border-radius:8px; }
            QMenu { background:$surface; color:$text; border:1px solid $border; }
            QMenu::item:selected { background:$selection; }
        '''))
        self.canvas = MarkupCanvas()
        self.canvas.installEventFilter(self)
        self.canvas.changed.connect(self.sync_tools)
        self.canvas.text_requested.connect(self.add_text)
        self.tools = {}
        self.bar_moved = False
        outer = QVBoxLayout(self); outer.setContentsMargins(12,10,12,10); outer.setSpacing(8)
        title = self.title = QLabel('标记截图'); title.setObjectName('markupTitle')
        title.setFont(emphasis_font())
        outer.addWidget(title)
        self.view = QScrollArea(); self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setWidget(self.canvas); outer.addWidget(self.view,1)
        self.bar = self.build_bar()
        toolbar = self.bar.layout()
        self.bar_widgets = [toolbar.itemAt(index).widget() for index in range(toolbar.count())]
        self.bar_columns = len(self.bar_widgets)
        self.bar.setFixedSize(self.bar.sizeHint())
        self.wide_bar_width = self.bar.width()
        outer.setContentsMargins(12, 10, 12, self.bar.height() + 16)
        for sequence, action in (('Escape',self.cancelled.emit),('Ctrl+Z',self.canvas.undo),
                                 ('Ctrl+Shift+Z',self.canvas.redo),('Ctrl+Y',self.canvas.redo)):
            shortcut = QShortcut(QKeySequence(sequence),self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut); shortcut.activated.connect(action)

    def build_bar(self):
        bar = QFrame(self); bar.setObjectName('markupTools')
        tools = QGridLayout(bar); tools.setContentsMargins(3,2,3,2); tools.setSpacing(3)

        def add(control):
            tools.addWidget(control,0,tools.count(),Qt.AlignmentFlag.AlignCenter)

        def button(glyph, label, action=None):
            control = QPushButton()
            control.setFixedSize(34,34); control.setIconSize(QSize(20,20))
            control.setAccessibleName(label); control.setToolTip(label)
            if glyph:
                color = '#FFFFFF' if glyph == 'done' else COLORS['muted']
                control.setIcon(tool_icon(TOOL_GLYPHS[glyph], color))
            if action: control.clicked.connect(action)
            add(control)
            return control

        def mode(name, label):
            control = button(name, label, lambda: self.select_tool(name))
            control.setCheckable(True)
            self.tools[name] = control

        def divider():
            line = QWidget(); line.setObjectName('markupDivider'); line.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
            line.setFixedSize(1,20); add(line)

        grip = button('grip','拖动标记工具栏'); grip.setFixedWidth(22)
        grip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.bar_drag = WindowDrag(grip, bar, self.move_bar)
        mode('pointer','指针 · 拖动文字')
        mode('pen','画笔')
        mode('text','文字标记')
        self.shape_button = button('shape','形状 · 直线、矩形、椭圆')
        self.shape_button.setCheckable(True)
        self.shape_menu = QMenu(self.shape_button)
        for name, label in SHAPES.items():
            self.shape_menu.addAction(label, lambda checked=False, selected=name: self.select_tool(selected))
        self.shape_button.setMenu(self.shape_menu)
        self.color_button = button(None,'标记颜色')
        self.color_button.setMenu(self.build_palette())
        self.set_color(DEFAULT_COLOR)
        mode('erase','橡皮擦 · 按住拖动擦除笔迹')
        erase_menu = QMenu(self.tools['erase'])
        erase_menu.addAction('清除全部标记', lambda: self.canvas.commit(()) if self.canvas.marks else None)
        self.tools['erase'].setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tools['erase'].customContextMenuRequested.connect(lambda pos: erase_menu.exec(self.tools['erase'].mapToGlobal(pos)))
        divider()
        self.undo_button = button('undo','撤销',self.canvas.undo)
        self.redo_button = button('redo','重做',self.canvas.redo)
        divider()
        button('close','关闭标记并放弃修改',self.cancelled.emit)
        done = self.done_button = button('done','完成',lambda: self.accepted.emit(self.canvas.marks))
        done.setObjectName('markupDone')
        return bar

    def build_palette(self):
        menu = QMenu(self.color_button)
        menu.setStyleSheet(themed('QMenu { background:$surface; border:1px solid $border; padding:2px; }'))
        panel = QWidget()
        panel.setStyleSheet(themed('''
            QPushButton { background:transparent; border:2px solid transparent; border-radius:16px; padding:0; }
            QPushButton:hover, QPushButton:focus { border-color:$gray; }
            QPushButton:checked { border-color:$primary; }
        '''))
        grid = QGridLayout(panel); grid.setContentsMargins(5,5,5,5); grid.setSpacing(3)
        self.color_choices = {}
        for index, (label, color) in enumerate((('自定义颜色', None), *PALETTE)):
            chip = QPushButton()
            chip.setFixedSize(32,32); chip.setIconSize(QSize(26,26))
            chip.setAccessibleName(label)
            chip.setIcon(self.swatch(color) if color else self.color_wheel())
            chip.setCheckable(color is not None)
            chip.clicked.connect(lambda checked=False, selected=color: self.choose_color(menu, selected))
            grid.addWidget(chip, index//3, index%3)
            if color: self.color_choices[color] = chip
        action = QWidgetAction(menu)
        action.setDefaultWidget(panel)
        menu.addAction(action)
        return menu

    @staticmethod
    def color_wheel():
        pixmap = QPixmap(72,72); pixmap.setDevicePixelRatio(3)
        pixmap.fill(Qt.GlobalColor.transparent)
        gradient = QConicalGradient(12,12,0)
        for index, color in enumerate(('#326AFF','#18C6E0','#68DD36','#FCE34A','#FF6F36','#E92D9C','#326AFF')):
            gradient.setColorAt(index/6, QColor(color))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(gradient)
        painter.drawEllipse(QRectF(2,2,20,20)); painter.end()
        return QIcon(pixmap)

    def choose_color(self, menu, color):
        menu.close()
        if color is None:
            selected = QColorDialog.getColor(QColor(self.canvas.color), self, '自定义颜色')
            if not selected.isValid(): return
            color = selected.name().upper()
        self.set_color(color)

    @staticmethod
    def swatch(color):
        return tool_icon(f'<circle cx="12" cy="12" r="9.7" fill="{color}" stroke="{COLORS["gray"]}" '
                         'stroke-width=".65"/>', color)

    def set_color(self, color):
        self.canvas.color = color
        self.color_button.setIcon(self.swatch(color))
        for value, chip in self.color_choices.items():
            chip.setChecked(value == color)

    def add_text(self, point):
        text, accepted = QInputDialog.getText(self, '文字标记', '标记内容')
        if accepted and text.strip() and self.canvas.image is not None:
            mark = Mark('text', (point,), self.canvas.color, text.strip())
            self.canvas.commit((*self.canvas.marks, mark))

    def open(self, image, marks, *, preview=False):
        self.title.setText('标记预览图' if preview else '标记截图')
        done_text = '完成并加入聊天' if preview else '完成'
        self.done_button.setAccessibleName(done_text)
        self.done_button.setToolTip(done_text)
        self.canvas.load(image,marks)
        self.select_tool('pen')
        self.bar_moved = False
        self.place_bar()
        self.fit()
        self.canvas.setFocus()

    def select_tool(self, tool):
        self.canvas.tool = tool
        self.canvas.setCursor(Qt.CursorShape.ArrowCursor if tool == 'pointer' else Qt.CursorShape.CrossCursor)
        for name, button in self.tools.items(): button.setChecked(name==tool)
        self.shape_button.setChecked(tool in SHAPES)

    def sync_tools(self):
        self.undo_button.setEnabled(bool(self.canvas.undo_stack))
        self.redo_button.setEnabled(bool(self.canvas.redo_stack))

    def fit(self):
        if self.canvas.image is not None:
            viewport = self.view.maximumViewportSize()
            image = self.canvas.image
            self.canvas.scale_to(min(4.,max(1,viewport.width()-8)/image.width(),
                                     max(1,viewport.height()-8)/image.height()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow_bar()
        self.place_bar()
        self.fit()

    def reflow_bar(self):
        columns = len(self.bar_widgets) if self.width()-8 >= self.wide_bar_width else max(1,(self.width()-11)//37)
        if columns == self.bar_columns:
            return
        self.bar_columns = columns
        toolbar = self.bar.layout()
        for widget in self.bar_widgets:
            toolbar.removeWidget(widget)
        for index,widget in enumerate(self.bar_widgets):
            row,column = divmod(index,columns)
            toolbar.addWidget(widget,row,column,Qt.AlignmentFlag.AlignCenter)
        self.bar.setFixedSize(self.bar.sizeHint())
        self.layout().setContentsMargins(12,10,12,self.bar.height()+16)

    def place_bar(self):
        position = self.bar.pos() if self.bar_moved else QPoint(
            (self.width() - self.bar.width()) // 2, self.height() - self.bar.height() - 7)
        self.bar.move(constrain(position, self.bar.size(), self.rect().adjusted(4, 44, -4, -4)))
        self.bar.raise_()

    def move_bar(self, position):
        self.bar_moved = True
        self.bar.move(constrain(position, self.bar.size(), self.rect().adjusted(4, 44, -4, -4)))

    def eventFilter(self, watched, event):
        if watched is self.canvas and event.type()==QEvent.Type.KeyPress and event.key()==Qt.Key.Key_Escape:
            self.cancelled.emit()
            return True
        return super().eventFilter(watched,event)
