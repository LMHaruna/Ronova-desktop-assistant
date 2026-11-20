"""Scrollable desktop partitions for the original Windows Explorer items."""

from dataclasses import dataclass, field, replace
import ctypes
import json
from math import ceil, floor
import os
import stat
import time
from pathlib import Path
import uuid

from PySide6.QtCore import (QEasingCurve, QEvent, QMimeData, QObject, QPoint, QPointF,
                            QRect, QRectF, QSize, Qt, QThreadPool, QTimer, QUrl, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QCursor, QDrag, QGuiApplication, QIcon, QImage, QPainter,
                           QPainterPath, QPalette, QPen, QPixmap, QRegion, QTextLayout, QTextOption)
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog,
                               QLabel, QListView, QListWidget, QListWidgetItem, QMenu, QPushButton,
                               QSizePolicy, QSlider, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
                               QToolTip, QVBoxLayout, QWidget)

from client_desktop_native import (ExplorerDesktop, FVO_CUSTOMPOSITION, FWF_AUTOARRANGE,
                                   FWF_NOICONS, LAYOUT_FLAGS, SHELL_ID_LIST_MIME, desktop_icon_details)
from client_shell import WINDOW_FLAGS, WindowDrag, constrain
from client_theme import COLORS, themed


FAVORITES = 'favorites'
UNASSIGNED = 'unassigned'
MACHINE_NAME = '本机配置'
OUTSIDE = 'desktop'
DEFAULT_TRANSPARENCY = 80
DEFAULT_GROUPS = ((UNASSIGNED, '未分类'), ('ai', 'AI'), ('work', '工作'), ('network', '网络'), ('games', '游戏'))
PANEL_FLAGS = WINDOW_FLAGS | Qt.WindowType.WindowStaysOnBottomHint
ITEM_MIME = 'application/x-ronova-desktop-items'
ITEM_KEY = Qt.ItemDataRole.UserRole
PANEL_GAP = 9
ALIGN_DISTANCE = 12
BOTTOM_DISTANCE = 24
GRID_FLEX = 10
SNAP_RELEASE = 18
SNAP_DURATION = 72
SYSTEM_ICONS = {
    '59031a47-3f72-44a7-89c5-5595fe6b30ee',
    '20d04fe0-3aea-1069-a2d8-08002b30309d',
    '645ff040-5081-101b-9f08-00aa002f954e',
}
STYLE = themed("""
QWidget { background: transparent; color: #f8f6fa; }
QPushButton { border: 1px solid rgba(255,255,255,56); border-radius: 6px; padding: 4px 7px; }
QPushButton:hover { background: rgba(255,255,255,35); }
QSlider::groove:horizontal { height: 4px; border-radius: 2px; background: rgba(255,255,255,68); }
QSlider::handle:horizontal { width: 12px; margin: -4px 0; border-radius: 6px; background: #e6b1c4; }
QMenu, QDialog { background: #252128; color: white; }
QMenu::item:selected { background: #6f4657; }
QLineEdit { background: #252128; border: 1px solid #746570; color: white; padding: 4px; }
QListWidget { border: none; outline: none; }
QListWidget::item { border: 1px solid transparent; border-radius: 6px; }
QListWidget::item:hover { background: rgba(255,255,255,24); }
QListWidget::item:selected { background: $selection; border-color: $border; }
""")


def path_key(path):
    return os.path.normcase(os.path.normpath(path))


def desktop_folder_id(key):
    if not Path(key).is_absolute():
        return None
    try:
        info = os.stat(key)
    except OSError:
        return None
    if stat.S_ISDIR(info.st_mode):
        # On the bundled Windows Python 3.11, st_ctime is the creation time.
        return info.st_dev, info.st_ino, info.st_ctime_ns
    return None


@dataclass(frozen=True)
class PanelSettings:
    bounds: tuple | None = None
    transparency: int = DEFAULT_TRANSPARENCY
    width: int | None = None

    @classmethod
    def load(cls, values):
        if not isinstance(values, dict):
            return cls()
        bounds = values.get('bounds')
        if not (isinstance(bounds, list) and len(bounds) == 4 and
                all(type(value) is int and abs(value) <= 100000 for value in bounds) and
                1 <= bounds[2] <= 3 and bounds[3] > 0):
            bounds = None
        transparency = values.get('transparency')
        width = values.get('width')
        return cls(tuple(bounds) if bounds else None,
                   max(0, min(100, transparency)) if type(transparency) is int else DEFAULT_TRANSPARENCY,
                   width if type(width) is int and 0 < width <= 100000 else None)


@dataclass(frozen=True)
class OrganizerLayout:
    groups: tuple = DEFAULT_GROUPS
    placements: dict = field(default_factory=dict)
    panels: dict = field(default_factory=dict)
    order: dict = field(default_factory=dict)
    outside_by_default: bool = False
    layers: tuple = ()
    folder_ids: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path):
        try:
            values = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(values, dict):
                return cls()
        except (OSError, ValueError):
            return cls()
        groups = []
        known = {FAVORITES, OUTSIDE}
        raw_groups = values.get('groups')
        if not isinstance(raw_groups, list):
            raw_groups = DEFAULT_GROUPS
        elif values.get('version') not in (2, 3):
            raw_groups = [DEFAULT_GROUPS[0], *raw_groups]
        for item in raw_groups:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            key, name = item
            if isinstance(key, str) and key and key not in known and isinstance(name, str) and name.strip():
                groups.append((key, name.strip()[:28]))
                known.add(key)
        known.add(UNASSIGNED)
        assignments = values.get('placements', {})
        placements = {path_key(file): group if group in known else UNASSIGNED
                      for file, group in assignments.items()
                      if isinstance(group, str) and (Path(file).is_absolute() or file.startswith('::'))} if isinstance(assignments, dict) else {}
        saved_panels = values.get('panels')
        panels = {key: PanelSettings.load(saved_panels[key]) for key in (FAVORITES, *(key for key, _ in groups))
                  if key in saved_panels} if isinstance(saved_panels, dict) else {}
        saved_order = values.get('order')
        order = {}
        if isinstance(saved_order, dict):
            for key in (FAVORITES, *(key for key, _ in groups)):
                entries = saved_order.get(key)
                if isinstance(entries, list):
                    order[key] = tuple(dict.fromkeys(path_key(item) for item in entries if isinstance(item, str)))
        saved_layers = values.get('layers')
        panel_keys = {FAVORITES, *(key for key, _ in groups)}
        layers = tuple(dict.fromkeys(key for key in saved_layers
                                     if isinstance(key, str) and key in panel_keys)) if isinstance(saved_layers, list) else ()
        raw_folder_ids = values.get('folder_ids')
        folder_ids = {path_key(key): tuple(identity) for key, identity in raw_folder_ids.items()
                      if placements.get(path_key(key)) not in (None, OUTSIDE) and isinstance(identity, list)
                      and len(identity) == 3 and all(type(value) is int for value in identity)
                      } if isinstance(raw_folder_ids, dict) else {}
        return cls(tuple(groups), placements, panels, order, values.get('version') == 3, layers, folder_ids)

    def save(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        panels = {key: {'bounds': settings.bounds, 'transparency': settings.transparency, 'width': settings.width}
                  for key, settings in self.panels.items()}
        version = 3 if self.outside_by_default else 2
        temporary.write_text(json.dumps({'version': version, 'groups': self.groups, 'placements': self.placements,
                                         'panels': panels, 'order': self.order, 'layers': self.layers,
                                         'folder_ids': self.folder_ids},
                                        ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)


def wallpaper_image(size):
    if os.name != 'nt' or size.isEmpty():
        return QImage()
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.user32.SystemParametersInfoW(0x0073, len(buffer), buffer, 0) or not buffer.value:
        return QImage()
    image = QImage(buffer.value)
    if image.isNull():
        return image
    # Soften the wallpaper once for all panels, in screen coordinates.
    small = QSize(max(1, size.width() // 20), max(1, size.height() // 20))
    image = image.scaled(small, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    image = image.copy((image.width() - small.width()) // 2, (image.height() - small.height()) // 2,
                       small.width(), small.height()).scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio,
                                                             Qt.TransformationMode.SmoothTransformation)
    image = image.convertToFormat(QImage.Format.Format_RGB32)
    tint = QPainter(image)
    tint.fillRect(image.rect(), QColor(0, 0, 0, 190))
    tint.end()
    return image


class PanelResize(QWidget):
    def __init__(self, panel, edges, cursor):
        super().__init__(panel)
        self.panel = panel
        self.edges = edges
        self.press = None
        self.delta = None
        self.setCursor(cursor)
        self.setToolTip('拖动调整分区大小')

    def paintEvent(self, event):
        painter = QPainter(self)
        # A nonzero alpha keeps the edge reachable on a layered Windows window.
        painter.fillRect(self.rect(), QColor(255, 255, 255, 8))
        if self.edges == (Qt.Edge.RightEdge | Qt.Edge.BottomEdge):
            painter.setPen(QPen(QColor(240, 235, 240, 145), 1))
            painter.drawLine(4, 11, 11, 4)
            painter.drawLine(8, 11, 11, 8)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.panel.complete_snap()
            self.panel.resize_magnets.clear()
            self.press = event.globalPosition().toPoint()
            self.original = self.panel.geometry()
            self.row_height = self.panel.items.preferred_row_height
            self.delta = None
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.delta = event.globalPosition().toPoint() - self.press
            self.panel.resize_from(self.original, self.edges, self.delta, self.row_height)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.press is not None:
            self.finish_resize()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def event(self, event):
        if event.type() in (QEvent.Type.UngrabMouse, QEvent.Type.Hide) and getattr(self, 'press', None) is not None:
            self.finish_resize()
        return super().event(event)

    def finish_resize(self):
        self.press = None
        if self.delta is not None:
            self.panel.resize_from(self.original, self.edges, self.delta, self.row_height, final=True)
        self.panel.resize_magnets.clear()


def partition_drag_keys(mime):
    if not mime.hasFormat(ITEM_MIME):
        return ()
    try:
        keys = json.loads(bytes(mime.data(ITEM_MIME)))
    except (UnicodeError, ValueError):
        return ()
    return tuple(key for key in keys if isinstance(key, str)) if isinstance(keys, list) else ()


class DesktopDropArea(QWidget):
    """A temporary drop target for the uncovered desktop during a partition drag."""

    def __init__(self, screen, panels, keys, desktop, native_icons):
        super().__init__(None, PANEL_FLAGS | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.keys = keys
        self.destination = None
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.setScreen(screen)
        bounds = screen.availableGeometry()
        self.setGeometry(bounds)
        region = QRegion(QRect(QPoint(), bounds.size()))
        for panel in panels:
            if panel.isVisible():
                region -= QRegion(panel.geometry().translated(-bounds.topLeft()))
        # Let Explorer receive the OLE drop over icons that still belong to its view.
        x, y, _, _ = desktop.window_bounds(int(self.winId()))
        scale = self.devicePixelRatioF()
        for left, top, right, bottom in native_icons:
            l, t = floor((left - x) / scale), floor((top - y) / scale)
            r, b = ceil((right - x) / scale), ceil((bottom - y) / scale)
            region -= QRegion(QRect(l, t, r - l, b - t))
        self.setMask(region)
        if not region.isEmpty():
            self.show()

    def paintEvent(self, event):
        # Windows sends drag events only to nontransparent layered pixels.
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor(0, 0, 0, 1))

    def dragEnterEvent(self, event):
        if event.possibleActions() & Qt.DropAction.MoveAction and partition_drag_keys(event.mimeData()) == self.keys:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        self.dragEnterEvent(event)
        if event.isAccepted():
            self.destination = event.position().toPoint()


class PanelItemDelegate(QStyledItemDelegate):
    TOP = 5
    BOTTOM = 7

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.gap = 3 if view.panel.key == FAVORITES else 6
        self.labels = {}

    def label_layout(self, text):
        font = self.view.font()
        width = self.view.gridSize().width() - 12
        key = text, font.key(), width
        if key not in self.labels:
            layout = QTextLayout(text, font)
            options = QTextOption()
            options.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            options.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            layout.setTextOption(options)
            layout.beginLayout()
            height = 0.
            while (line := layout.createLine()).isValid():
                line.setLineWidth(width)
                line.setPosition(QPointF(0, height))
                height += line.height()
            layout.endLayout()
            self.labels[key] = layout, ceil(height)
        return self.labels[key]

    def sizeHint(self, option, index):
        return self.view.gridSize()

    def paint(self, painter, option, index):
        appearance = QStyleOptionViewItem(option)
        self.initStyleOption(appearance, index)
        text = appearance.text
        appearance.text = ''
        appearance.icon = QIcon()
        appearance.showDecorationSelected = True
        painter.save()
        self.view.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, appearance, painter, self.view)
        painter.restore()
        painter.save()
        painter.setOpacity(1.0)
        rect = option.rect
        artwork = index.data(Qt.ItemDataRole.DecorationRole)
        top = rect.top() + self.TOP
        if isinstance(artwork, QPixmap):
            size = artwork.deviceIndependentSize()
            painter.drawPixmap(QPointF(rect.center().x() - size.width() / 2, top), artwork)
            icon_height = size.height()
        else:
            size = self.view.iconSize()
            if isinstance(artwork, QIcon):
                artwork.paint(painter, QRect(rect.center().x() - size.width() // 2, top, size.width(), size.height()))
            icon_height = size.height()
        label, _ = self.label_layout(text)
        selected = appearance.state & QStyle.StateFlag.State_Selected
        painter.setPen(QColor(COLORS['text']) if selected else appearance.palette.color(QPalette.ColorRole.Text))
        label.draw(painter, QPointF(rect.left() + 6, top + icon_height + self.gap))
        painter.restore()


class PanelItems(QListWidget):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.entries = {}
        self.drop_hint = QRect()
        self.drop_on_icon = False
        self.icon_drop_key = None
        self.icon_drop = None
        self.drag_point = QPoint()
        self.drag_keys = ()
        self.drag_scroll = QTimer(self, interval=55)
        self.drag_scroll.timeout.connect(self.scroll_for_drag)
        self.measure_labels = QTimer(self, singleShot=True, interval=0)
        self.measure_labels.timeout.connect(self.update_row_height)
        self.setAccessibleName('分区图标')
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setWrapping(True)
        self.setWordWrap(True)
        self.setUniformItemSizes(True)
        self.setGridSize(panel.grid)
        self.preferred_row_height = panel.grid.height()
        self.tiles = PanelItemDelegate(self)
        self.setItemDelegate(self.tiles)
        self.update_icon_size()
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setMovement(QListView.Movement.Static)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.itemActivated.connect(lambda item: panel.organizer.open_item(item.data(ITEM_KEY)))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.item_menu)

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            for key in self.entries:
                self.refresh_entry(key)
        elif event.type() == QEvent.Type.FontChange and hasattr(self, 'tiles'):
            self.tiles.labels.clear()
            self.measure_labels.start()
        return result

    def update_icon_size(self):
        grid = self.panel.grid
        size = min(self.panel.organizer.icon_size, grid.width() - 16, grid.height() - 37)
        self.setIconSize(QSize(size, size))
        self.measure_labels.start()

    def update_row_height(self):
        panel = self.panel
        columns = panel.layout_columns(panel.width())
        width_delta = panel.width() - panel.width_for(columns)
        width_delta = width_delta if abs(width_delta) <= GRID_FLEX else 0
        width = panel.grid.width() + width_delta // columns
        if width != self.gridSize().width():
            self.setGridSize(QSize(width, self.gridSize().height()))
        labels = max((self.tiles.label_layout(item.text())[1] for item in self.entries.values()),
                     default=self.fontMetrics().height())
        needed = self.tiles.TOP + self.iconSize().height() + self.tiles.gap + labels + self.tiles.BOTTOM
        default = 0 if panel.key == FAVORITES else panel.grid.height()
        self.preferred_row_height = max(needed, default)
        body = panel.height() - panel.EXTRA_HEIGHT
        rows = max(1, round(body / self.preferred_row_height))
        if abs(body - rows * self.preferred_row_height) <= GRID_FLEX and body // rows >= needed:
            height = body // rows
        else:
            height = self.preferred_row_height
        if height != self.gridSize().height():
            self.setGridSize(QSize(width, height))
        panel.setMinimumHeight(min(panel.minimumHeight(), height + panel.EXTRA_HEIGHT))

    def show_entries(self, keys):
        if tuple(self.entries) == keys:
            return
        selected = {item.data(ITEM_KEY) for item in self.selectedItems()}
        scrollbar = self.verticalScrollBar()
        scroll = scrollbar.value()
        self.clear()
        self.entries.clear()
        self.tiles.labels.clear()
        for key in keys:
            item = QListWidgetItem()
            item.setData(ITEM_KEY, key)
            self.entries[key] = item
            self.addItem(item)
            self.refresh_entry(key)
            item.setSelected(key in selected)
        self.update_row_height()
        self.doItemsLayout()
        scrollbar.setValue(scroll)

    def refresh_entry(self, key):
        item = self.entries.get(key)
        if item is not None:
            name, artwork = self.panel.organizer.item_details(key, self)
            if name != item.text():
                self.measure_labels.start()
            item.setText(name)
            item.setToolTip(name)
            # QPixmap decoration uses its real size. QIcon alone stretches a
            # smaller raster to the requested logical size on high-DPI screens.
            item.setData(Qt.ItemDataRole.DecorationRole, artwork)

    def startDrag(self, actions):
        selected = sorted(self.selectedItems(), key=self.row)
        desktop = self.panel.organizer.desktop
        if selected and desktop is not None:
            drag = QDrag(self)
            mime = QMimeData()
            keys = tuple(item.data(ITEM_KEY) for item in selected)
            mime.setData(ITEM_MIME, json.dumps(keys).encode('utf-8'))
            if all(Path(key).is_absolute() for key in keys):
                mime.setUrls([QUrl.fromLocalFile(key) for key in keys])
            shell_ids = desktop.shell_id_list(keys)
            if shell_ids:
                mime.setData(SHELL_ID_LIST_MIME, shell_ids)
            drag.setMimeData(mime)
            artwork = selected[0].data(Qt.ItemDataRole.DecorationRole)
            preview = artwork if isinstance(artwork, QPixmap) else artwork.pixmap(self.iconSize(), self.devicePixelRatioF())
            drag.setPixmap(preview)
            size = preview.deviceIndependentSize()
            drag.setHotSpot(QPointF(size.width() / 2, size.height() / 2).toPoint())
            self.panel.organizer.drag_items(drag, keys)

    def drop_keys(self, event):
        mime = event.mimeData()
        if mime.hasFormat(ITEM_MIME):
            return partition_drag_keys(mime)
        if mime.hasUrls():
            return tuple(path_key(url.toLocalFile()) for url in mime.urls() if url.isLocalFile())
        return ()

    def drop_action(self, event):
        # External Explorer drops only update the category. Never ask the source
        # to remove or move the underlying desktop file after accepting a drop.
        if event.mimeData().hasFormat(ITEM_MIME):
            return Qt.DropAction.MoveAction
        return Qt.DropAction.CopyAction

    def insertion_at(self, point, keys):
        columns = self.panel.layout_columns(self.panel.width())
        width, height = self.gridSize().toTuple()
        scroll = self.verticalScrollBar().value()
        cell = self.indexAt(point)
        if cell.isValid():
            index = cell.row()
        else:
            row = max(0, (point.y() + scroll) // height)
            column = max(0, min(columns - 1, point.x() // width))
            index = min(self.count(), row * columns + column)
        # Dropping a local item onto a later cell puts it in that cell after
        # removing it from its old position. Items from another box insert before.
        after = index < self.count() and any(key in self.entries and self.row(self.entries[key]) < index
                                             for key in keys)
        anchor = min(index, max(0, self.count() - 1))
        row, column = divmod(anchor, columns)
        x, y = column * width, row * height - scroll
        trailing = after or (index == self.count() and self.count() > 0)
        if columns == 1:
            hint = QRect(x + 7, y + (height - 4 if trailing else 2), width - 14, 2)
        else:
            hint = QRect(x + (width - 4 if trailing else 2), y + 7, 2, height - 14)
        return index + int(after), hint

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.drop_hint.isNull():
            painter = QPainter(self.viewport())
            if self.drop_on_icon:
                painter.setPen(QColor(COLORS['primary']))
                painter.drawRoundedRect(self.drop_hint, 5, 5)
            else:
                painter.fillRect(self.drop_hint, QColor(COLORS['primary']))

    def set_drop_hint(self, hint, *, on_icon=False):
        if hint != self.drop_hint or on_icon != self.drop_on_icon:
            old = self.drop_hint
            self.drop_hint = hint
            self.drop_on_icon = on_icon
            self.viewport().update(old.united(hint).adjusted(-1, -1, 1, 1))

    def update_drop_hint(self):
        _, hint = self.insertion_at(self.drag_point, self.drag_keys)
        self.set_drop_hint(hint)

    def drag_direction(self):
        edge = 22
        if self.drag_point.y() < edge:
            return -1
        if self.drag_point.y() >= self.viewport().height() - edge:
            return 1
        return 0

    def scroll_for_drag(self):
        bar = self.verticalScrollBar()
        value = bar.value()
        bar.setValue(value + self.drag_direction() * max(8, self.gridSize().height() // 6))
        self.update_drop_hint()
        if bar.value() == value:
            self.drag_scroll.stop()

    def clear_drop_hint(self):
        self.drag_scroll.stop()
        self.set_drop_hint(QRect())

    def clear_icon_drop(self):
        self.icon_drop_key = None
        operation, self.icon_drop = self.icon_drop, None
        if operation is not None:
            operation.close()

    def icon_drop_item(self, point):
        item = self.itemAt(point)
        if item is None:
            return None
        rect = self.visualItemRect(item)
        artwork = item.data(Qt.ItemDataRole.DecorationRole)
        size = artwork.deviceIndependentSize().toSize() if isinstance(artwork, QPixmap) else self.iconSize()
        icon = QRect(QPoint(rect.center().x() - size.width() // 2, rect.top() + self.tiles.TOP), size)
        if icon.adjusted(-4, -4, 4, 4).contains(point):
            return item
        label, _ = self.tiles.label_layout(item.text())
        label_point = QPointF(point) - QPointF(rect.left() + 6, icon.bottom() + 1 + self.tiles.gap)
        if any(label.lineAt(i).naturalTextRect().adjusted(-2, 0, 2, 0).contains(label_point)
               for i in range(label.lineCount())):
            return item
        return None

    @staticmethod
    def shell_drag_state(event, desktop):
        buttons, modifiers = event.buttons(), event.modifiers()
        state = sum(flag for button, flag in ((Qt.MouseButton.LeftButton, 1), (Qt.MouseButton.RightButton, 2),
                                              (Qt.MouseButton.MiddleButton, 16)) if buttons & button)
        state |= sum(flag for modifier, flag in ((Qt.KeyboardModifier.ShiftModifier, 4),
                                                (Qt.KeyboardModifier.ControlModifier, 8),
                                                (Qt.KeyboardModifier.AltModifier, 32)) if modifiers & modifier)
        return state, desktop.cursor_position(), event.possibleActions().value & 7

    def shell_action(self, event, keys):
        desktop = self.panel.organizer.desktop
        item = self.icon_drop_item(event.position().toPoint())
        target = item.data(ITEM_KEY) if item is not None else None
        if not keys or desktop is None or target is None or target in keys:
            self.clear_icon_drop()
            return Qt.DropAction.IgnoreAction
        identity = target, keys
        try:
            if identity != self.icon_drop_key:
                self.clear_icon_drop()
                self.icon_drop_key = identity
                self.icon_drop = desktop.item_drop(target, keys, int(self.panel.winId()))
            if self.icon_drop is not None:
                effect = self.icon_drop.update(*self.shell_drag_state(event, desktop))
                for action in (Qt.DropAction.MoveAction, Qt.DropAction.LinkAction, Qt.DropAction.CopyAction):
                    if effect & action.value:
                        self.drag_scroll.stop()
                        self.set_drop_hint(self.visualItemRect(item).adjusted(2, 2, -2, -2), on_icon=True)
                        return action
        except OSError:
            self.clear_icon_drop()
            self.icon_drop_key = identity
        return Qt.DropAction.IgnoreAction

    def dragEnterEvent(self, event):
        self.dragMoveEvent(event)
        if not event.isAccepted() and self.drop_keys(event):
            # A file from Explorer can cross the partition's empty cells before
            # it reaches an icon that accepts it.
            event.setDropAction(Qt.DropAction.IgnoreAction)
            event.accept()

    def dragMoveEvent(self, event):
        keys = self.drop_keys(event)
        action = self.shell_action(event, keys)
        if action != Qt.DropAction.IgnoreAction:
            event.setDropAction(action)
            event.accept()
            return
        action = self.drop_action(event)
        if not event.mimeData().hasFormat(ITEM_MIME):
            self.panel.organizer.discover_drop(keys)
        if not (event.possibleActions() & action and any(key in self.panel.organizer.items for key in keys)):
            self.clear_drop_hint()
            event.ignore()
            return
        self.drag_point = event.position().toPoint()
        self.drag_keys = keys
        self.update_drop_hint()
        if self.drag_direction() and self.verticalScrollBar().maximum() > 0:
            if not self.drag_scroll.isActive():
                self.drag_scroll.start()
        else:
            self.drag_scroll.stop()
        event.setDropAction(action)
        event.accept()

    def dragLeaveEvent(self, event):
        self.clear_icon_drop()
        self.clear_drop_hint()
        super().dragLeaveEvent(event)

    def hideEvent(self, event):
        self.clear_icon_drop()
        self.clear_drop_hint()
        super().hideEvent(event)

    def dropEvent(self, event):
        keys = self.drop_keys(event)
        action = self.shell_action(event, keys)
        self.clear_drop_hint()
        if action != Qt.DropAction.IgnoreAction:
            organizer = self.panel.organizer
            operation, self.icon_drop = self.icon_drop, None
            self.icon_drop_key = None
            event.ignore()
            try:
                if operation.drop(*self.shell_drag_state(event, organizer.desktop)):
                    # The Shell acts on the real files. An external source must
                    # never delete them again based on a reported MoveAction.
                    if action == Qt.DropAction.MoveAction and not event.mimeData().hasFormat(ITEM_MIME):
                        action = Qt.DropAction.CopyAction
                    if event.possibleActions() & action:
                        event.setDropAction(action)
                        event.accept()
                QTimer.singleShot(0, organizer.refresh_items)
            except OSError as error:
                organizer.show_error(f'无法完成 Windows 桌面拖放：{error}')
            finally:
                operation.close()
            return
        self.clear_icon_drop()
        action = self.drop_action(event)
        if not event.mimeData().hasFormat(ITEM_MIME):
            self.panel.organizer.discover_drop(keys)
        index, _ = self.insertion_at(event.position().toPoint(), keys)
        if event.possibleActions() & action and self.panel.organizer.move_items(keys, self.panel.key, index):
            event.setDropAction(action)
            event.accept()
        else:
            event.ignore()

    def item_menu(self, point):
        item = self.itemAt(point)
        if item is None:
            return
        selected = sorted(self.selectedItems(), key=self.row) if item.isSelected() else [item]
        keys = tuple(entry.data(ITEM_KEY) for entry in selected)
        self.panel.organizer.item_menu(self.panel, keys, self.viewport().mapTo(self.panel, point))


class PanelBackdrop(QWidget):
    """Paint the wallpaper and tint below the independently painted icons and labels."""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAutoFillBackground(False)
        self.setGeometry(panel.rect())
        self.lower()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 11, 11)
        panel = self.panel
        transparency = panel.transparency if panel.preview_transparency is None else panel.preview_transparency
        opacity = (100 - transparency) / 100
        image, origin = panel.organizer.wallpaper_for(panel.geometry().center())
        source = QRect(panel.pos() - origin, self.size())
        has_wallpaper = not image.isNull() and image.rect().contains(source)
        painter.fillPath(shape, QColor(0, 0, 2, 2 if has_wallpaper else max(2, round(opacity * 255))))
        if opacity and has_wallpaper:
            painter.save()
            painter.setClipPath(shape)
            painter.setOpacity(opacity)
            painter.drawImage(self.rect(), image, source)
            painter.restore()
        painter.setPen(QPen(QColor(245, 238, 246, 78), 1))
        painter.drawPath(shape)

class GroupPanel(QWidget):
    # The native icon grid starts below the header and its outer margins.
    EXTRA_WIDTH = 23
    EXTRA_HEIGHT = 44
    EDGE = 6

    def __init__(self, organizer, key, name, grid, settings):
        super().__init__(None, PANEL_FLAGS)
        self.organizer = organizer
        self.key = key
        self.transparency = settings.transparency
        self.preview_transparency = None
        self.grid = grid
        self.max_columns = 1 if key == FAVORITES else 3
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        font = QGuiApplication.font()
        font.setPixelSize(13)
        self.setFont(font)
        self.setStyleSheet(STYLE)
        self.setMinimumSize(self.width_for(1) - GRID_FLEX, grid.height() + self.EXTRA_HEIGHT - GRID_FLEX)
        self.setMaximumWidth(self.width_for(self.max_columns) + GRID_FLEX)
        self.backdrop = PanelBackdrop(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 4, 7, 9)
        layout.setSpacing(2)
        header = QWidget()
        self.header = header
        header.setObjectName('partitionHeader')
        header.setStyleSheet('QWidget#partitionHeader { background-color: rgba(255,255,255,8); border-radius: 5px; }')
        header.setFixedHeight(29)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(2, 0, 0, 0)
        header_layout.setSpacing(3)
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        title_font = self.font()
        title_font.setPixelSize(15)
        title_font.setBold(True)
        self.title.setFont(title_font)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        header_layout.addWidget(self.title, 1)
        self.menu = QPushButton('...')
        self.menu.setFixedSize(26, 23)
        self.menu.setToolTip('分区选项')
        if key == FAVORITES:
            self.menu.clicked.connect(organizer.favorites_menu)
        else:
            self.menu.clicked.connect(lambda: organizer.group_menu(self))
        header_layout.addWidget(self.menu)
        layout.addWidget(header)
        self.drag = WindowDrag(header, self, self.move_partition, started=self.begin_move)
        self.items = PanelItems(self)
        layout.addWidget(self.items, 1)
        self.set_name(name)
        self.handles = [
            PanelResize(self, Qt.Edge.LeftEdge, Qt.CursorShape.SizeHorCursor),
            PanelResize(self, Qt.Edge.RightEdge, Qt.CursorShape.SizeHorCursor),
            PanelResize(self, Qt.Edge.TopEdge, Qt.CursorShape.SizeVerCursor),
            PanelResize(self, Qt.Edge.BottomEdge, Qt.CursorShape.SizeVerCursor),
            PanelResize(self, Qt.Edge.RightEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeFDiagCursor),
        ]
        self.save_timer = QTimer(self, singleShot=True, interval=200)
        self.save_timer.timeout.connect(lambda: organizer.save_panel(self))
        self.resize_magnets = {}
        self.snap_raw = QRect()
        self.snap_edges = ()
        self.snap_origin = ()
        self.snap_overlap = False
        self.snap_animation = QVariantAnimation(self)
        self.snap_animation.setStartValue(0.0)
        self.snap_animation.setEndValue(1.0)
        self.snap_animation.setDuration(SNAP_DURATION)
        self.snap_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.snap_animation.valueChanged.connect(self.render_snap)

    def set_name(self, name):
        self.title.setText(name)
        self.setWindowTitle(name)
        self.menu.setAccessibleName(f'{name}分区选项')
        self.items.setAccessibleName(f'{name}图标')

    def width_for(self, columns):
        width = self.grid.width() * columns + self.EXTRA_WIDTH
        return max(116, width) if self.key == FAVORITES else width

    def columns_for(self, width):
        return max(1, min(self.max_columns, round((width - self.EXTRA_WIDTH) / self.grid.width())))

    def layout_columns(self, width):
        nearest = self.columns_for(width)
        if abs(width - self.width_for(nearest)) <= GRID_FLEX:
            return nearest
        return max(1, min(self.max_columns, (width - self.EXTRA_WIDTH) // self.grid.width()))

    def rows_for(self, height):
        return max(1, round((height - self.EXTRA_HEIGHT) / self.items.preferred_row_height))

    def height_for(self, rows):
        return rows * self.items.preferred_row_height + self.EXTRA_HEIGHT

    def resize_to(self, width, height):
        self.resize(self.width_for(self.columns_for(width)), self.height_for(self.rows_for(height)))

    def restore_size(self, settings):
        _, _, columns, height = settings.bounds
        width = self.width_for(min(columns, self.max_columns))
        if settings.width is not None and self.layout_columns(settings.width) == min(columns, self.max_columns):
            width = settings.width
        self.resize(width, height)

    def fits(self, bounds):
        return (self.minimumWidth() <= bounds.width() <= self.maximumWidth()
                and bounds.height() >= self.minimumHeight())

    def begin_move(self):
        self.complete_snap()
        self.move_size = self.size()
        self.organizer.raise_panel(self)

    def move_partition(self, point):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        size = self.move_size if self.drag.dragging else self.size()
        raw = QRect(constrain(point, size, area), size)
        self.follow_bounds(raw, self.organizer.align_partition(self, raw, area), allow_overlap=True)

    def resize_from(self, original, edges, delta, row_height, *, final=False):
        screen = QGuiApplication.screenAt(original.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        left, top = bool(edges & Qt.Edge.LeftEdge), bool(edges & Qt.Edge.TopEdge)
        horizontal = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
        vertical = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
        width, height = original.width(), original.height()
        grid_width, grid_height = width, height
        if horizontal:
            room = original.right() - area.left() + 1 if left else area.right() - original.left() + 1
            width = min(room, self.maximumWidth(), max(self.minimumWidth(), width + (-delta.x() if left else delta.x())))
            columns = self.columns_for(width)
            while columns > 1 and self.width_for(columns) > room:
                columns -= 1
            grid_width = self.width_for(columns)
        if vertical:
            room = original.bottom() - area.top() + 1 if top else area.bottom() - original.top() + 1
            height = min(room, max(self.minimumHeight(), height + (-delta.y() if top else delta.y())))
            max_rows = max(1, (room - self.EXTRA_HEIGHT) // row_height)
            rows = max(1, min(max_rows, round((height - self.EXTRA_HEIGHT) / row_height)))
            grid_height = self.EXTRA_HEIGHT + rows * row_height

        def anchored(size):
            point = QPoint(original.right() - size.width() + 1 if left else original.left(),
                           original.bottom() - size.height() + 1 if top else original.top())
            return QRect(point, size)

        raw = anchored(QSize(width, height))
        grid = anchored(QSize(grid_width, grid_height))
        target = self.organizer.snap_resized_partition(self, raw, grid, area, edges, final=final)
        self.follow_bounds(raw, target)

    def follow_bounds(self, raw, target, *, allow_overlap=False):
        raw_edges = raw.getCoords()
        magnets = tuple((index, value) for index, value in enumerate(target.getCoords()) if value != raw_edges[index])
        self.snap_raw = raw
        self.snap_overlap = allow_overlap
        if magnets != self.snap_edges:
            self.snap_animation.stop()
            self.snap_edges = magnets
            self.snap_origin = tuple(previous if abs(previous - current) <= SNAP_RELEASE else current
                                     for previous, current in zip(self.geometry().getCoords(), raw_edges))
            if magnets:
                self.snap_animation.start()
        self.render_snap(self.snap_animation.currentValue())

    def render_snap(self, progress):
        coords = list(self.snap_raw.getCoords())
        for index, target in self.snap_edges:
            start = self.snap_origin[index]
            coords[index] = round(start + (target - start) * progress)
        bounds = QRect()
        bounds.setCoords(*coords)
        self.apply_bounds(bounds, allow_overlap=self.snap_overlap)

    def complete_snap(self):
        if self.snap_animation.state() == QVariantAnimation.State.Running:
            self.snap_animation.stop()
            self.render_snap(1.0)
        self.snap_edges = ()

    def apply_bounds(self, bounds, *, allow_overlap=False):
        if bounds == self.geometry() or not self.fits(bounds):
            return
        if allow_overlap or self.organizer.room_for(self, bounds):
            self.setGeometry(bounds)

    def resizeEvent(self, event):
        width, height, edge = self.width(), self.height(), self.EDGE
        strips = (QRect(0, 0, edge, height), QRect(width-edge, 0, edge, height),
                  QRect(0, 0, width, edge), QRect(0, height-edge, width, edge),
                  QRect(width-15, height-15, 15, 15))
        for handle, strip in zip(getattr(self, 'handles', ()), strips):
            handle.setGeometry(strip)
            handle.raise_()
        if hasattr(self, 'backdrop'):
            self.backdrop.setGeometry(self.rect())
        if hasattr(self, 'items'):
            self.items.update_row_height()
        if self.isVisible():
            self.save_timer.start()
        super().resizeEvent(event)

    def moveEvent(self, event):
        if self.isVisible():
            self.save_timer.start()
        if hasattr(self, 'backdrop'):
            self.backdrop.update()
        super().moveEvent(event)

    def bounds(self):
        return self.x(), self.y(), self.layout_columns(self.width()), self.height()


class DesktopOrganizer(QObject):
    icon_ready = Signal(int, str, str, QImage)

    def __init__(self, root):
        super().__init__()
        self.state_path = Path(root) / 'data' / 'desktop-organizer.json'
        self.restore_path = self.state_path.with_name('desktop-organizer-restore.json')
        self.state = OrganizerLayout.load(self.state_path)
        self.front_order = self.state.layers
        self.desktop = None
        self.wallpapers = {}
        self.panels = {}
        self.favorites = None
        self.items = {}
        self.masked_icons = {}
        self.original_clip = None
        self.masked_size = None
        self.applied_clip = None
        self.restore_auto_arrange = False
        self.last_drop_probe = ()
        self.icons = {}
        self.icon_size = 32
        self.icon_scale = 1.
        self.icon_generation = 0
        self.capture_hidden = False
        self.icon_ready.connect(self.save_icon, Qt.ConnectionType.QueuedConnection)
        self.watch = QTimer(self, interval=1200)
        self.watch.timeout.connect(self.refresh_items)

    def windows(self):
        if self.favorites is None:
            return ()
        return (self.favorites, *self.panels.values())

    def hide(self):
        self.watch.stop()
        self.capture_hidden = False
        for panel in self.windows():
            panel.complete_snap()
            if panel.save_timer.isActive():
                panel.save_timer.stop()
                self.save_panel(panel)
            panel.hide()
        self.items.clear()
        self.masked_icons.clear()
        self.original_clip = None
        self.masked_size = None
        self.applied_clip = None
        self.restore_auto_arrange = False
        self.last_drop_probe = ()
        self.icons.clear()
        self.icon_generation += 1
        if self.desktop is not None:
            self.desktop.close()
            self.desktop = None
            if not self.recover():
                self.show_error('暂时无法恢复原桌面图标，下次启动时会重试')

    def recover(self):
        if not self.restore_path.is_file():
            return True
        try:
            desktop = ExplorerDesktop()
            try:
                self.restore_desktop(desktop)
            finally:
                desktop.close()
        except (OSError, ValueError, TypeError, KeyError):
            return False
        return True

    def restore_desktop(self, desktop):
        if not self.restore_path.is_file():
            return
        saved = json.loads(self.restore_path.read_text(encoding='utf-8'))
        if not isinstance(saved, dict):
            raise ValueError('桌面图标恢复记录格式无效')
        if type(saved.get('icons_visible')) is bool:
            show_icons = saved['icons_visible'] and not (desktop.flags()[0] & FWF_NOICONS)
            desktop.set_icons_visible(show_icons)
            self.restore_path.unlink()
            return
        if saved.get('masked_layout') is True:
            clip = saved.get('original_clip')
            if clip is not None and (not isinstance(clip, list) or any(
                    not isinstance(rect, list) or len(rect) != 4 or any(type(n) is not int for n in rect)
                    for rect in clip)):
                raise ValueError('桌面图标区域备份格式无效')
            desktop.set_icon_region(clip)
            if saved.get('restore_auto_arrange') is True:
                flags, options = desktop.flags()
                desktop.set_flags(flags | FWF_AUTOARRANGE, options)
            self.restore_path.unlink()
            return
        positions = saved['positions']
        if not isinstance(positions, dict):
            raise ValueError('桌面位置备份格式无效')
        for point in positions.values():
            if not isinstance(point, list) or len(point) != 2 or any(type(n) is not int or abs(n) > 100000 for n in point):
                raise ValueError('桌面位置备份坐标无效')
        if saved.get('parked_layout') is True:
            flags, options = desktop.flags()
            if flags & FWF_AUTOARRANGE:
                desktop.set_flags(flags & ~FWF_AUTOARRANGE, options)
            try:
                desktop.positions(positions)
            finally:
                if saved.get('restore_auto_arrange') is True:
                    flags |= FWF_AUTOARRANGE
                if flags & FWF_AUTOARRANGE:
                    desktop.set_flags(flags, options)
        else:
            # Restore a legacy organizer session that positioned all desktop items.
            flags, options = saved['flags'], saved['options']
            if type(flags) is not int or type(options) is not int:
                raise ValueError('桌面位置备份格式无效')
            desktop.set_flags(flags & ~LAYOUT_FLAGS, options | FVO_CUSTOMPOSITION)
            try:
                desktop.positions(positions)
            finally:
                desktop.set_flags(flags, options)
        self.restore_path.unlink()

    def save_recovery(self):
        self.restore_path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.restore_path.with_suffix('.tmp')
        pending.write_text(json.dumps({'masked_layout': True, 'original_clip': self.original_clip,
                                       'restore_auto_arrange': self.restore_auto_arrange},
                                      ensure_ascii=False), encoding='utf-8')
        pending.replace(self.restore_path)

    def initial_assignments(self, items):
        if self.state.outside_by_default:
            return
        fallback = UNASSIGNED if UNASSIGNED in dict(self.state.groups) else next(
            (key for key, _ in self.state.groups), FAVORITES)
        defaults = {key: FAVORITES if self.system_item(key) else fallback for key in items
                    if key not in self.state.placements and desktop_folder_id(key) is None}
        migrated = replace(self.state, placements={**defaults, **self.state.placements}, outside_by_default=True)
        if not self.commit(migrated):
            raise OSError('无法保存现有桌面分类')

    def check_folder_placements(self, items, *, opening=False):
        folder_ids = dict(self.state.folder_ids)
        replaced = set()
        for key in items.keys() & self.state.placements.keys():
            previous = folder_ids.get(key)
            if self.state.placements[key] == OUTSIDE or (previous is None and key in self.items):
                continue
            current = desktop_folder_id(key)
            if current is None:
                continue
            if current == previous or (previous is None and opening):
                folder_ids[key] = current
            else:
                # Explorer can reuse the name of a deleted folder for a new item.
                replaced.add(key)
                folder_ids.pop(key, None)
        if replaced or folder_ids != self.state.folder_ids:
            placements = {key: group for key, group in self.state.placements.items() if key not in replaced}
            order = {group: tuple(key for key in keys if key not in replaced)
                     for group, keys in self.state.order.items()}
            if not self.commit(replace(self.state, placements=placements, order=order, folder_ids=folder_ids)):
                raise OSError('无法保存桌面文件夹的分区状态')
        return replaced

    @staticmethod
    def system_item(key):
        return key.removeprefix('::{').split('}', 1)[0] in SYSTEM_ICONS

    def create_favorites(self):
        settings = self.state.panels.get(FAVORITES, PanelSettings())
        self.favorites = GroupPanel(self, FAVORITES, MACHINE_NAME, self.grid, settings)
        if settings.bounds:
            x, y, _, _ = settings.bounds
            self.favorites.restore_size(settings)
            point = QPoint(x, y)
            screen = QGuiApplication.screenAt(point)
            area = screen.availableGeometry() if screen else self.area
            self.favorites.move(constrain(point, self.favorites.size(), area))
        else:
            rows = max(1, (self.area.height() - self.favorites.EXTRA_HEIGHT) // self.grid.height())
            self.favorites.resize_to(self.favorites.width_for(1), self.favorites.height_for(rows))
            self.favorites.move(self.area.topLeft())

    def favorites_menu(self):
        panel = self.favorites
        menu = QMenu(panel)
        menu.setFont(panel.font())
        create = menu.addAction('新建分区')
        transparency = menu.addAction('调整背景透明度')
        action = menu.exec(panel.menu.mapToGlobal(panel.menu.rect().bottomLeft()))
        if action is create:
            self.add_group(panel)
        elif action is transparency:
            self.change_transparency(panel)

    def toggle(self, anchor):
        if self.desktop is not None:
            self.hide()
            return
        try:
            self.desktop = ExplorerDesktop()
            self.restore_desktop(self.desktop)
            flags, options = self.desktop.flags()
            if flags & FWF_NOICONS or not self.desktop.icons_visible():
                raise OSError('请先在桌面右键菜单的「查看」中开启「显示桌面图标」')
            items = self.desktop.positions()
            self.initial_assignments(items)
            self.check_folder_placements(items, opening=True)
            self.original_clip = self.desktop.icon_region()
            self.restore_auto_arrange = bool(flags & FWF_AUTOARRANGE)
            self.save_recovery()
            if self.restore_auto_arrange:
                self.desktop.set_flags(flags & ~FWF_AUTOARRANGE, options)
                if self.desktop.flags()[0] & FWF_AUTOARRANGE:
                    raise OSError('无法暂时关闭 Windows 自动排列图标')
            screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
            self.area = screen.availableGeometry().adjusted(10, 10, -10, -10)
            self.icon_scale = screen.devicePixelRatio()
            self.icon_size = self.desktop.icon_size()
            width, height = self.desktop.spacing()
            self.grid = QSize(max(76, round(width / self.icon_scale) + 7),
                              max(94, round(height / self.icon_scale) + 7))
            self.wallpapers.clear()
            if self.favorites is None:
                self.create_favorites()
            for panel in self.windows():
                panel.items.update_icon_size()
            self.set_items(items)
            self.normalize_layout()
            for panel in self.windows():
                panel.show()
            self.restore_panel_order()
            self.watch.start()
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.hide()
            self.show_error(f'无法开启桌面整理：{error}')

    def wallpaper_for(self, point):
        screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
        rect = screen.geometry()
        key = rect.getRect()
        if key not in self.wallpapers:
            self.wallpapers[key] = wallpaper_image(rect.size())
        return self.wallpapers[key], rect.topLeft()

    def free_position(self, size):
        start_x = self.area.left() + self.favorites.width() + PANEL_GAP
        occupied = [panel.geometry() for panel in self.windows()]
        columns = sorted({start_x, *(rect.right() + 1 + PANEL_GAP for rect in occupied)})
        rows = sorted({self.area.top(), *(rect.bottom() + 1 + PANEL_GAP for rect in occupied)})
        for x in columns:
            for y in rows:
                point = QPoint(x, y)
                candidate = QRect(point, size)
                if x >= start_x and self.area.contains(candidate) and not any(candidate.intersects(rect) for rect in occupied):
                    return point
        return None

    def add_panel(self, key, name, count):
        settings = self.state.panels.get(key, PanelSettings())
        panel = GroupPanel(self, key, name, self.grid, settings)
        if settings.bounds:
            x, y, _, _ = settings.bounds
            panel.restore_size(settings)
            point = QPoint(x, y)
            screen = QGuiApplication.screenAt(point)
            area = screen.availableGeometry() if screen else self.area
            panel.move(constrain(point, panel.size(), area))
        else:
            max_rows = max(1, (self.area.height() - panel.EXTRA_HEIGHT) // self.grid.height())
            wanted_rows = max(1, min(max_rows, (count + 2) // 3))
            for columns in (3, 2, 1):
                for rows in range(wanted_rows, 0, -1):
                    panel.resize_to(panel.width_for(columns), panel.height_for(rows))
                    point = self.free_position(panel.size())
                    if point is not None:
                        panel.move(point)
                        break
                else:
                    continue
                break
            else:
                panel.move(constrain(self.favorites.geometry().topRight() + QPoint(PANEL_GAP, PANEL_GAP),
                                     panel.size(), self.area))
        self.panels[key] = panel
        if self.watch.isActive():
            panel.show()
        return panel

    @staticmethod
    def show_error(text):
        QToolTip.showText(QCursor.pos(), text)

    def commit(self, new_state, *, refresh=False):
        try:
            new_state.save(self.state_path)
        except OSError:
            self.show_error('桌面整理布局保存失败')
            return False
        previous = self.state
        self.state = new_state
        if refresh and self.desktop is not None:
            try:
                self.arrange()
            except OSError as error:
                try:
                    previous.save(self.state_path)
                    self.state = previous
                except OSError:
                    pass
                self.hide()
                self.show_error(f'无法整理桌面图标：{error}')
                return False
        return True

    def raise_panel(self, panel, *, remember=True):
        if not panel.isVisible():
            return
        if self.desktop is not None and QGuiApplication.platformName() == 'windows':
            siblings = [int(other.winId()) for other in self.windows() if other is not panel and other.isVisible()]
            try:
                self.desktop.raise_partition(int(panel.winId()), siblings)
            except OSError as error:
                self.show_error(f'无法调整分区层级：{error}')
                return
        else:
            panel.raise_()
        if remember:
            self.front_order = (*[key for key in self.front_order if key != panel.key], panel.key)
            panel.save_timer.start()

    def restore_panel_order(self):
        panels = {panel.key: panel for panel in self.windows()}
        for key in self.front_order:
            if key in panels:
                self.raise_panel(panels[key], remember=False)

    def room_for(self, panel, bounds):
        # Allow an overlapping saved layout to be pulled apart after a screen size change.
        return all(other is panel or not other.isVisible() or not bounds.intersects(other.geometry())
                   or panel.geometry().intersected(other.geometry()).contains(bounds.intersected(other.geometry()))
                   for other in self.windows())

    @staticmethod
    def shares_row(first, second):
        overlap = min(first.bottom(), second.bottom()) - max(first.top(), second.top()) + 1
        return overlap >= min(first.height(), second.height()) / 3

    @staticmethod
    def shares_column(first, second):
        overlap = min(first.right(), second.right()) - max(first.left(), second.left()) + 1
        return overlap >= min(first.width(), second.width()) / 2

    def neighboring_bounds(self, panel, area):
        return [other.geometry() for other in self.windows()
                if other is not panel and other.isVisible() and other.geometry().intersects(area)]

    def align_partition(self, panel, bounds, area):
        neighbors = self.neighboring_bounds(panel, area)
        if any(bounds.intersects(rect) for rect in neighbors):
            return bounds

        def fits(candidate):
            return area.contains(candidate) and panel.fits(candidate) and self.room_for(panel, candidate)

        sides, stack = [], []
        for rect in neighbors:
            if self.shares_row(bounds, rect):
                left = rect.center().x() < bounds.center().x()
                x = rect.right() + 1 + PANEL_GAP if left else rect.left() - PANEL_GAP - bounds.width()
                if abs(x - bounds.left()) <= ALIGN_DISTANCE:
                    sides.append((x, left, rect))
            if self.shares_column(bounds, rect):
                above = rect.center().y() < bounds.center().y()
                y = rect.bottom() + 1 + PANEL_GAP if above else rect.top() - PANEL_GAP - bounds.height()
                if abs(y - bounds.top()) <= ALIGN_DISTANCE:
                    stack.append((y, above, rect))
        sides.sort(key=lambda entry: abs(entry[0] - bounds.left()))
        stack.sort(key=lambda entry: abs(entry[0] - bounds.top()))

        for x, left, side in sides:
            if not left:
                continue
            for _, on_left, right in sides:
                width = right.left() - PANEL_GAP - x
                if on_left or abs(width - bounds.width()) > GRID_FLEX:
                    continue
                if abs(side.bottom() - right.bottom()) <= GRID_FLEX:
                    candidate = QRect(x, side.bottom() - bounds.height() + 1, width, bounds.height())
                    if abs(candidate.top() - bounds.top()) <= BOTTOM_DISTANCE and fits(candidate):
                        return candidate

        for y, above, _ in stack:
            for x, _, side in sides:
                candidate = QRect(QPoint(x, y), bounds.size())
                if above and abs(candidate.bottom() - side.bottom()) <= GRID_FLEX:
                    candidate.setBottom(side.bottom())
                    if fits(candidate):
                        return candidate

        for y, _, rect in stack:
            candidate = QRect(QPoint(bounds.left(), y), bounds.size())
            if abs(bounds.left() - rect.left()) <= ALIGN_DISTANCE:
                candidate.moveLeft(rect.left())
            elif abs(bounds.right() - rect.right()) <= ALIGN_DISTANCE:
                candidate.moveRight(rect.right())
            if abs(candidate.width() - rect.width()) <= GRID_FLEX and candidate.left() == rect.left():
                candidate.setWidth(rect.width())
            if fits(candidate):
                return candidate

        for x, _, side in sides:
            dy = side.bottom() - bounds.bottom()
            if abs(dy) <= BOTTOM_DISTANCE:
                candidate = bounds.translated(x - bounds.left(), dy)
                if fits(candidate):
                    return candidate

        for x, _, _ in sides:
            candidate = bounds.translated(x - bounds.left(), 0)
            if fits(candidate):
                return candidate

        work = area.adjusted(10, 10, -10, -10)
        x = min((work.left() - bounds.left(), work.right() - bounds.right()), key=abs)
        y = min((work.top() - bounds.top(), work.bottom() - bounds.bottom()), key=abs)
        candidate = bounds.translated(x if abs(x) <= ALIGN_DISTANCE else 0, y if abs(y) <= ALIGN_DISTANCE else 0)
        return candidate if fits(candidate) else bounds

    def snap_resized_partition(self, panel, bounds, grid, area, edges, *, final=False):
        candidate = QRect(bounds)
        targets = {edge: [] for edge in (Qt.Edge.LeftEdge, Qt.Edge.TopEdge, Qt.Edge.RightEdge, Qt.Edge.BottomEdge)}
        left, top, right, bottom = targets
        for rect in self.neighboring_bounds(panel, area):
            if self.shares_row(bounds, rect):
                on_left = rect.center().x() < bounds.center().x()
                gap = bounds.left() - rect.right() - 1 if on_left else rect.left() - bounds.right() - 1
                if abs(gap - PANEL_GAP) <= SNAP_RELEASE:
                    targets[bottom].append(rect.bottom())
                    targets[top].append(rect.top())
                    targets[left if on_left else right].append(
                        rect.right() + 1 + PANEL_GAP if on_left else rect.left() - 1 - PANEL_GAP)
            if self.shares_column(bounds, rect):
                above = rect.center().y() < bounds.center().y()
                gap = bounds.top() - rect.bottom() - 1 if above else rect.top() - bounds.bottom() - 1
                if abs(gap - PANEL_GAP) <= SNAP_RELEASE:
                    targets[left].append(rect.left())
                    targets[right].append(rect.right())
                    targets[top if above else bottom].append(
                        rect.bottom() + 1 + PANEL_GAP if above else rect.top() - 1 - PANEL_GAP)
        for edge, getter, setter in ((left, QRect.left, QRect.setLeft), (top, QRect.top, QRect.setTop),
                                     (right, QRect.right, QRect.setRight), (bottom, QRect.bottom, QRect.setBottom)):
            if not edges & edge:
                continue
            current = getter(bounds)
            nominal = getter(grid)
            previous = panel.resize_magnets.pop(edge, None)
            nearby = sorted({value for value in targets[edge] if abs(value - current) <= ALIGN_DISTANCE},
                            key=lambda value: abs(value - current))
            choices = []
            if previous in targets[edge] and abs(previous - current) <= SNAP_RELEASE:
                choices.append(previous)
            choices.extend(nearby)
            if final or abs(nominal - current) <= GRID_FLEX or (
                    previous == nominal and abs(nominal - current) <= SNAP_RELEASE):
                choices.append(nominal)
            for target in dict.fromkeys(choices):
                adjusted = QRect(candidate)
                setter(adjusted, target)
                if area.contains(adjusted) and panel.fits(adjusted) and self.room_for(panel, adjusted):
                    candidate = adjusted
                    panel.resize_magnets[edge] = target
                    break
        return candidate if area.contains(candidate) else panel.geometry()

    def align_stack_bottoms(self, layout, above_by_panel, followed, windows):
        # An adjacent tall panel can follow the whole stack when changing the
        # last short panel would require cutting a row of icons.
        standalone = [panel for panel in windows if panel not in above_by_panel and panel not in followed]
        tolerance = max(BOTTOM_DISTANCE, self.grid.height() // 2)
        for tail in above_by_panel:
            if tail in followed:
                continue
            head = tail
            while head in above_by_panel:
                head = above_by_panel[head]
            first, last = layout[head], layout[tail]
            candidates = []
            for panel in standalone:
                rect = layout[panel]
                gap = max(first.left() - rect.right() - 1, rect.left() - first.right() - 1)
                bottom_delta = abs(last.bottom() - rect.bottom())
                if (gap < 0 or abs(gap - PANEL_GAP) > ALIGN_DISTANCE or
                        abs(first.top() - rect.top()) > ALIGN_DISTANCE or bottom_delta > tolerance):
                    continue
                aligned = QRect(rect)
                aligned.setTop(first.top())
                aligned.setBottom(last.bottom())
                screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
                if panel.fits(aligned) and screen.availableGeometry().contains(aligned):
                    candidates.append((abs(gap - PANEL_GAP), bottom_delta, panel, aligned))
            if candidates:
                _, _, panel, aligned = min(candidates, key=lambda candidate: candidate[:2])
                layout[panel] = aligned
                standalone.remove(panel)

    def normalize_layout(self):
        # Use saved adjacency for the whole pass so correcting one gap cannot
        # move the next frame out of snapping range.
        windows = self.windows()
        before = {panel: panel.geometry() for panel in windows}
        layout = {panel: QRect(rect) for panel, rect in before.items()}
        windows = tuple(panel for panel in windows if not any(
            other is not panel and before[panel].intersects(rect) for other, rect in before.items()))
        followed = set()
        above_by_panel = {}
        top_order = sorted(windows, key=lambda item: (before[item].top(), before[item].left()))
        for panel in top_order:
            old, rect = before[panel], layout[panel]
            above = [other for other in windows if other is not panel
                     and before[other].top() < old.top()
                     and self.shares_column(old, before[other])
                     and abs(old.left() - before[other].left()) <= ALIGN_DISTANCE
                     and abs(old.top() - before[other].bottom() - 1 - PANEL_GAP) <= ALIGN_DISTANCE]
            if above:
                previous = min(above, key=lambda other: abs(old.top() - before[other].bottom() - 1 - PANEL_GAP))
                anchor = layout[previous]
                rect.moveTopLeft(QPoint(anchor.left(), anchor.bottom() + 1 + PANEL_GAP))
                if abs(rect.width() - anchor.width()) <= GRID_FLEX:
                    rect.setWidth(anchor.width())
                followed.add(previous)
                above_by_panel[panel] = previous

        for panel in sorted(windows, key=lambda item: (before[item].left(), before[item].top())):
            old, rect = before[panel], layout[panel]
            left = [other for other in windows if other is not panel
                    and before[other].left() < old.left()
                    and self.shares_row(old, before[other])
                    and abs(old.left() - before[other].right() - 1 - PANEL_GAP) <= ALIGN_DISTANCE]
            if not left:
                continue
            previous = min(left, key=lambda other: abs(old.left() - before[other].right() - 1 - PANEL_GAP))
            anchor = layout[previous]
            if panel not in above_by_panel:
                rect.moveLeft(anchor.right() + 1 + PANEL_GAP)
            if panel in followed:
                continue
            bottom_delta = abs(rect.bottom() - anchor.bottom())
            if panel in above_by_panel or previous in followed:
                if bottom_delta <= GRID_FLEX:
                    rect.setBottom(anchor.bottom())
            elif bottom_delta <= BOTTOM_DISTANCE:
                rect.moveBottom(anchor.bottom())

        for panel in top_order:
            if panel in above_by_panel:
                layout[panel].moveLeft(layout[above_by_panel[panel]].left())

        self.align_stack_bottoms(layout, above_by_panel, followed, windows)

        for panel, rect in layout.items():
            screen = QGuiApplication.screenAt(before[panel].center()) or QGuiApplication.primaryScreen()
            if rect != before[panel] and (not screen.availableGeometry().contains(rect) or not panel.fits(rect)):
                return
            for other, other_rect in layout.items():
                if other is not panel and rect.intersects(other_rect) and not before[panel].intersects(before[other]):
                    return
        changed = {panel: rect for panel, rect in layout.items() if rect != before[panel]}
        if changed:
            for panel, rect in changed.items():
                panel.setGeometry(rect)
            settings = {panel.key: self.panel_settings(panel) for panel in changed}
            self.commit(replace(self.state, panels={**self.state.panels, **settings}))

    @staticmethod
    def panel_settings(panel):
        bounds = panel.bounds()
        width = panel.width() if panel.width() != panel.width_for(bounds[2]) else None
        return PanelSettings(bounds, panel.transparency, width)

    def save_panel(self, panel):
        if panel not in self.windows():
            return False
        settings = self.panel_settings(panel)
        if self.state.panels.get(panel.key) != settings or self.state.layers != self.front_order:
            return self.commit(replace(self.state, panels={**self.state.panels, panel.key: settings},
                                       layers=self.front_order))
        return True

    def set_capture_hidden(self, hidden):
        if self.desktop is None or hidden == self.capture_hidden:
            return
        self.capture_hidden = hidden
        try:
            if self.desktop.flags()[0] & (FWF_NOICONS | FWF_AUTOARRANGE):
                self.hide()
            else:
                excluded = () if hidden else tuple(self.masked_icons.values())
                self.desktop.set_icon_region(self.original_clip, excluded)
                if not hidden:
                    self.restore_panel_order()
        except OSError as error:
            self.hide()
            self.show_error(f'无法切换桌面图标层：{error}')

    def refresh_items(self, *, relocate_new=True):
        if self.desktop is None or self.capture_hidden:
            return
        if any(panel.drag.dragging or any(handle.press is not None for handle in panel.handles)
               for panel in self.windows()):
            return
        try:
            if self.desktop.flags()[0] & (FWF_NOICONS | FWF_AUTOARRANGE) or not self.desktop.icons_visible():
                self.hide()
                return
            items = self.desktop.positions()
            replaced_folders = self.check_folder_placements(items)
            if items != self.items or replaced_folders:
                self.set_items(items, relocate_new=relocate_new, replaced_folders=replaced_folders)
            elif self.masked_icons or self.desktop.desktop_size() != self.masked_size:
                self.sync_icon_mask(self.masked_icons)
        except OSError as error:
            self.hide()
            self.show_error(f'无法读取桌面图标：{error}')

    def discover_drop(self, keys):
        # A file can be dragged before the next periodic scan, especially just
        # after Windows has created or renamed it. Only accept Shell desktop items.
        if self.desktop is None or not keys or all(key in self.items for key in keys):
            return
        now = time.monotonic()
        if self.last_drop_probe and self.last_drop_probe[0] == keys and now - self.last_drop_probe[1] < .25:
            return
        self.last_drop_probe = keys, now
        self.refresh_items(relocate_new=False)

    def set_items(self, items, *, relocate_new=True, replaced_folders=()):
        new = tuple(key for key in items if key not in self.items or key in replaced_folders)
        self.items = items
        self.icons = {key: value for key, value in self.icons.items() if key in items and key not in replaced_folders}
        self.arrange(new if relocate_new else ())
        if new:
            generation = self.icon_generation
            icon_size = self.favorites.items.iconSize().width()
            limit = floor(icon_size * self.icon_scale)
            QThreadPool.globalInstance().start(lambda: self.load_icons(generation, new, limit))

    def load_icons(self, generation, keys, limit):
        try:
            for key, name, image in desktop_icon_details(keys, limit):
                if self.icon_generation != generation:
                    break
                self.icon_ready.emit(generation, key, name, image)
        except OSError:
            pass  # Generic icons remain usable if the Shell artwork is unavailable.

    def save_icon(self, generation, key, name, image):
        if generation == self.icon_generation and key in self.items:
            self.icons[key] = (name, QPixmap.fromImage(image))
            for panel in self.windows():
                panel.items.refresh_entry(key)

    def item_details(self, key, widget):
        name, source = self.icons.get(key, (Path(key).name, QPixmap()))
        if source.isNull():
            kind = QStyle.StandardPixmap.SP_DirIcon if key.startswith('::') else QStyle.StandardPixmap.SP_FileIcon
            return name, widget.style().standardIcon(kind)
        scale = widget.devicePixelRatioF()
        logical = max(1, min(widget.iconSize().width(), floor(min(source.width(), source.height()) / scale)))
        pixels = round(logical * scale)
        if source.size() == QSize(pixels, pixels):
            artwork = QPixmap(source)
        else:
            artwork = source.scaled(pixels, pixels, Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
        artwork.setDevicePixelRatio(scale)
        return name, artwork

    def open_item(self, key):
        try:
            if self.desktop is not None:
                self.desktop.open_item(key)
        except OSError as error:
            self.show_error(f'无法打开桌面项目：{error}')

    def sync_icon_mask(self, grouped):
        rectangles = self.desktop.icon_rectangles(grouped)
        size = self.desktop.desktop_size()
        current = self.desktop.icon_region() if rectangles else self.original_clip
        if rectangles != self.masked_icons or size != self.masked_size or current != self.applied_clip:
            self.desktop.set_icon_region(self.original_clip, tuple(rectangles.values()))
            self.masked_icons = rectangles
            self.masked_size = size
            self.applied_clip = self.desktop.icon_region()

    def drag_items(self, drag, keys):
        surfaces = []
        destination = None
        try:
            outside = self.desktop.positions().keys() - self.masked_icons.keys()
            native_icons = tuple(self.desktop.icon_rectangles(outside).values())
            for screen in QGuiApplication.screens():
                surfaces.append(DesktopDropArea(screen, self.windows(), keys, self.desktop, native_icons))
            actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction | Qt.DropAction.LinkAction
            if drag.exec(actions, Qt.DropAction.MoveAction) == Qt.DropAction.MoveAction and self.desktop is not None:
                dropped = next((area for area in surfaces if area.destination is not None), None)
                if dropped is not None:
                    x, y, _, _ = self.desktop.window_bounds(int(dropped.winId()))
                    scale = dropped.devicePixelRatioF()
                    destination = (x + round(dropped.destination.x() * scale),
                                   y + round(dropped.destination.y() * scale))
        except OSError as error:
            self.show_error(f'无法读取桌面落点：{error}')
        finally:
            for area in surfaces:
                area.hide()
                area.deleteLater()
        if destination is not None:
            self.move_to_desktop(keys, destination)
        else:
            self.refresh_items()

    def outside_moves(self, keys, grouped, destination=None):
        if not keys:
            return {}, set()
        bounds = [QRect(*self.desktop.window_bounds(int(panel.winId()))) for panel in self.windows()]
        bounds.extend(QRect(left, top, right - left, bottom - top)
                      for key, (left, top, right, bottom) in self.masked_icons.items() if key in grouped)
        width, height = self.desktop.desktop_size()
        dx, dy = self.desktop.spacing()
        dx, dy = max(32, dx), max(32, dy)

        def cell(point):
            return QRect(point[0] - dx // 4, point[1], dx, dy)

        def covered(point):
            return any(rect.intersects(cell(point)) for rect in bounds)

        if destination is None:
            native_bounds = self.desktop.icon_rectangles(keys)
            blocked = []
            for key in keys:
                if key not in self.items:
                    continue
                if key in native_bounds:
                    left, top, right, bottom = native_bounds[key]
                    icon = QRect(left, top, right - left, bottom - top)
                else:
                    icon = cell(self.items[key])
                if any(rect.intersects(icon) for rect in bounds):
                    blocked.append(key)
        else:
            blocked = [key for key in keys if key in self.items]
        if not blocked:
            return {}, set()
        occupied = [cell(point) for key, point in self.items.items() if key not in grouped and key not in blocked]
        slots = [(x, y) for x in range(dx // 4, width - dx, dx) for y in range(12, height - dy, dy)
                 if not covered((x, y)) and not any(rect.intersects(cell((x, y))) for rect in occupied)]
        moves = {}
        for key in blocked:
            if not slots:
                break
            x, y = destination if destination is not None else self.items[key]
            point = min(slots, key=lambda pos: abs(x - pos[0]) + abs(y - pos[1]))
            slots.remove(point)
            moves[key] = point
        return moves, set(blocked) - moves.keys()

    def arrange(self, new_items=()):
        groups = dict(self.state.groups)
        for key in self.panels.keys() - groups.keys():
            panel = self.panels.pop(key)
            panel.hide()
            panel.deleteLater()

        by_group = {FAVORITES: [], **{key: [] for key in groups}}
        fallback = UNASSIGNED if UNASSIGNED in groups else next(iter(groups), FAVORITES)

        def desktop_order(key):
            x, y = self.items[key]
            return y, x, key

        for key in sorted(self.items, key=desktop_order):
            default = FAVORITES if self.system_item(key) else OUTSIDE
            group = self.state.placements.get(key, default)
            if group != OUTSIDE:
                by_group[group if group in by_group else fallback].append(key)

        for group, keys in by_group.items():
            remaining = dict.fromkeys(keys)
            saved = tuple(key for key in self.state.order.get(group, ()) if key in remaining)
            for key in saved:
                remaining.pop(key)
            by_group[group] = (*saved, *remaining)

        self.favorites.items.show_entries(by_group[FAVORITES])
        # Reserve saved positions before fitting any new or unsaved panels.
        for key, name in groups.items():
            if key in self.state.panels and key not in self.panels:
                self.add_panel(key, name, len(by_group[key]))
        for key, name in groups.items():
            panel = self.panels.get(key) or self.add_panel(key, name, len(by_group[key]))
            panel.items.show_entries(by_group[key])

        grouped = {key for keys in by_group.values() for key in keys}
        self.sync_icon_mask(grouped)
        outside = tuple(key for key in new_items if key not in grouped)
        moves, _ = self.outside_moves(outside, grouped)
        if moves:
            self.desktop.position_icons(moves)
            self.items = self.desktop.positions()

    def item_menu(self, panel, keys, point):
        if self.desktop is None:
            return
        owner = int(panel.winId())
        scale = panel.devicePixelRatioF()
        try:
            action = self.desktop.show_item_menu(keys, owner, (round(point.x() * scale), round(point.y() * scale)))
            if action == 'rename' and len(keys) == 1:
                self.rename_item(panel, keys[0])
            elif action == 'refresh':
                self.refresh_items()
        except OSError as error:
            self.show_error(f'无法完成 Windows 桌面操作：{error}')

    def rename_item(self, panel, key):
        current = self.desktop.edit_name(key)
        name, confirmed = self.ask_name(panel, '重命名', '新名称', current)
        if not confirmed or not name or name == current or self.desktop is None:
            return
        updated = self.desktop.rename_item(key, name, int(panel.winId()))
        if updated != key:
            placements = dict(self.state.placements)
            placements[updated] = placements.pop(key, panel.key)
            order = {group: tuple(updated if item == key else item for item in entries)
                     for group, entries in self.state.order.items()}
            folder_ids = dict(self.state.folder_ids)
            identity = folder_ids.pop(key, None)
            if identity is not None:
                folder_ids[updated] = identity
            self.commit(replace(self.state, placements=placements, order=order, folder_ids=folder_ids))
        self.refresh_items()

    def move_to_desktop(self, keys, destination):
        keys = tuple(dict.fromkeys(key for key in keys if key in self.items))
        if not keys or self.desktop is None:
            return False
        grouped = self.masked_icons.keys() - set(keys)
        moves, blocked = self.outside_moves(keys, grouped, destination)
        if blocked:
            self.show_error('框外的桌面空位不足')
            return False
        previous_positions = {key: self.items[key] for key in moves}
        try:
            if moves:
                self.desktop.position_icons(moves)
                self.items = self.desktop.positions()
                if any(self.items.get(key) != point for key, point in moves.items()):
                    raise OSError('Windows 没有应用新的桌面图标位置')
            placements = {**self.state.placements, **dict.fromkeys(keys, OUTSIDE)}
            order = {group: tuple(item for item in entries if item not in keys)
                     for group, entries in self.state.order.items()}
            folder_ids = {key: identity for key, identity in self.state.folder_ids.items() if key not in keys}
            if self.commit(replace(self.state, placements=placements, order=order, folder_ids=folder_ids), refresh=True):
                return True
        except OSError as error:
            self.show_error(f'无法移出分区：{error}')
        if previous_positions:
            try:
                if self.desktop is not None:
                    self.desktop.position_icons(previous_positions)
                    self.refresh_items()
                else:
                    desktop = ExplorerDesktop()
                    try:
                        desktop.positions(previous_positions)
                    finally:
                        desktop.close()
            except OSError:
                self.show_error('暂时无法恢复原桌面图标位置')
        return False

    def move_items(self, keys, group, index=None):
        panel = self.favorites if group == FAVORITES else self.panels.get(group)
        keys = tuple(dict.fromkeys(key for key in keys if key in self.items))
        if panel is None or not keys:
            return False
        current = tuple(panel.items.entries)
        index = len(current) if index is None else max(0, min(index, len(current)))
        moving = set(keys)
        before = tuple(key for key in current[:index] if key not in moving)
        after = tuple(key for key in current[index:] if key not in moving)
        arranged = (*before, *keys, *after)
        if arranged == current:
            return True
        added = moving.difference(current)
        placements = {**self.state.placements, **dict.fromkeys(added, group)}
        order = {key: tuple(item for item in entries if item not in moving)
                 for key, entries in self.state.order.items()}
        hidden = tuple(key for key in order.get(group, ()) if key not in self.items)
        order[group] = (*arranged, *hidden)
        folder_ids = dict(self.state.folder_ids)
        for key in added:
            identity = desktop_folder_id(key)
            if identity is not None:
                folder_ids[key] = identity
        return self.commit(replace(self.state, placements=placements, order=order, folder_ids=folder_ids), refresh=True)

    @staticmethod
    def ask_name(parent, title, label, default):
        dialog = QInputDialog(parent)
        dialog.setFont(parent.font())
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        dialog.setTextValue(default)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        text = dialog.textValue()
        dialog.deleteLater()
        return text, accepted

    def group_name(self, parent, default=''):
        name, confirmed = self.ask_name(parent, '桌面分区', '分区名称', default)
        if not confirmed:
            return ''
        name = name.strip()[:28]
        occupied = {title.casefold() for _, title in self.state.groups if title != default}
        if name.casefold() in occupied or name == MACHINE_NAME:
            self.show_error('已有同名分区')
            return ''
        return name

    def add_group(self, parent):
        name = self.group_name(parent)
        if name:
            self.commit(replace(self.state, groups=(*self.state.groups, (uuid.uuid4().hex, name))), refresh=True)

    def rename_group(self, panel):
        name = self.group_name(panel, dict(self.state.groups)[panel.key])
        if name:
            groups = tuple((key, name if key == panel.key else title) for key, title in self.state.groups)
            if self.commit(replace(self.state, groups=groups)):
                panel.set_name(name)

    def delete_group(self, panel):
        groups = tuple((key, name) for key, name in self.state.groups if key != panel.key)
        fallback = UNASSIGNED if UNASSIGNED in dict(groups) else next((key for key, _ in groups), FAVORITES)
        placements = {path: fallback if key == panel.key else key for path, key in self.state.placements.items()}
        panels = {key: settings for key, settings in self.state.panels.items() if key != panel.key}
        order = {key: items for key, items in self.state.order.items() if key != panel.key}
        layers = tuple(key for key in self.front_order if key != panel.key)
        if self.commit(replace(self.state, groups=groups, placements=placements, panels=panels, order=order,
                               layers=layers), refresh=True):
            self.front_order = layers

    def change_transparency(self, panel):
        original = panel.transparency
        dialog = QDialog(panel)
        dialog.setFont(panel.font())
        dialog.setWindowTitle('分区背景透明度')
        dialog.setStyleSheet(STYLE)
        layout = QVBoxLayout(dialog)
        row = QHBoxLayout()
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setMinimumWidth(164)
        slider.setAccessibleName('分区背景透明度')
        slider.setValue(original)
        value = QLabel(f'{original}%')
        value.setMinimumWidth(35)
        row.addWidget(slider, 1)
        row.addWidget(value)
        layout.addLayout(row)
        buttons = QDialogButtonBox()
        accept = buttons.addButton('应用', QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton('取消', QDialogButtonBox.ButtonRole.RejectRole)
        accept.setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def preview(percent):
            panel.preview_transparency = percent
            panel.backdrop.update()
            value.setText(f'{percent}%')

        slider.valueChanged.connect(preview)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            panel.transparency = slider.value()
            if not self.save_panel(panel):
                panel.transparency = original
        panel.preview_transparency = None
        panel.backdrop.update()
        dialog.deleteLater()

    def group_menu(self, panel):
        menu = QMenu(panel)
        menu.setFont(panel.font())
        create = menu.addAction('新建分区')
        rename = menu.addAction('重命名分区')
        remove = menu.addAction('删除分区')
        transparency = menu.addAction('调整背景透明度')
        action = menu.exec(panel.menu.mapToGlobal(panel.menu.rect().bottomLeft()))
        if action is create:
            self.add_group(panel)
        elif action is rename:
            self.rename_group(panel)
        elif action is remove:
            self.delete_group(panel)
        elif action is transparency:
            self.change_transparency(panel)
