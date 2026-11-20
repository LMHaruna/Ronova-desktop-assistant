"""Small controls for the workspace sidebar and Codex composer."""
from math import isfinite
from pathlib import Path
from string import Template

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSignalBlocker, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap, QTransform
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu, QProgressBar, QPushButton, QSlider, QStyle,
                               QStyleOptionViewItem, QToolButton, QTreeWidget, QVBoxLayout, QWidget,
                               QWidgetAction)
from client_fonts import emphasis_font, typography
from client_panels import FoldoutChevron, ModelFoldout
from client_theme import COLORS, LIGHT_COLORS


def sidebar_colors():
    if COLORS['surface'] == LIGHT_COLORS['surface']:
        return LIGHT_COLORS
    return {**COLORS,
            'background': 'qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #302D2E,stop:0.45 #292627,stop:1 #262324)',
            'surface': '#343031', 'elevated': '#3B3738', 'selection': '#494244', 'text': '#F6F2F3',
            'muted': '#DED7D9', 'gray': '#CFC3C5', 'border': '#514A4D', 'primary': LIGHT_COLORS['primary']}


def tool_icon(glyph, color=COLORS['gray']):
    icon = QIcon()
    for mode, stroke in ((QIcon.Mode.Normal, color), (QIcon.Mode.Disabled, COLORS['disabled'])):
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
               f'<g fill="none" stroke="{stroke}" stroke-width="1.7" stroke-linecap="round" '
               f'stroke-linejoin="round">{glyph}</g></svg>')
        pixmap = QPixmap(72, 72)
        pixmap.setDevicePixelRatio(3)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, 24, 24))
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon


def tinted_svg_icon(path, replacements):
    try:
        svg = Path(path).read_text(encoding='utf-8')
    except OSError:
        return QIcon(str(path))
    for old, new in replacements.items():
        svg = svg.replace(old, new)
    pixmap = QPixmap(72, 72)
    pixmap.setDevicePixelRatio(3)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, 24, 24))
    painter.end()
    return QIcon(pixmap)


class LibrarySection(QFrame):
    def __init__(self, title, glyph, toggled):
        super().__init__()
        self.setObjectName('workLibrary')
        self.entries = None
        self.glyph = glyph
        self.icon = tool_icon(glyph,sidebar_colors()['muted'])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3,3,3,3)
        layout.setSpacing(0)
        self.header = QPushButton(self.icon,title)
        self.header.setObjectName('workLibraryHeader')
        self.header.setFont(emphasis_font())
        self.header.setCheckable(True)
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setMinimumHeight(30)
        arrow = self.arrow = FoldoutChevron()
        arrow.setProperty('chevronColor', sidebar_colors()['muted'])
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(0,0,8,0)
        header_layout.addStretch(1)
        header_layout.addWidget(arrow)
        layout.addWidget(self.header)

        body = QWidget()
        contents = QVBoxLayout(body)
        contents.setContentsMargins(3,0,3,2)
        self.items = QListWidget()
        self.items.setObjectName('workLibraryItems')
        self.items.setAccessibleName(title)
        self.items.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.items.setUniformItemSizes(True)
        self.items.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.items.setIconSize(QSize(18,18))
        contents.addWidget(self.items)
        self.empty = QLabel('本机未发现可列出的项目')
        self.empty.setObjectName('workHint')
        self.empty.setContentsMargins(6,5,6,5)
        contents.addWidget(self.empty)
        self.items.hide()
        typography().changed.connect(self.fit_items)

        foldout = ModelFoldout(body,arrow)
        self.header.toggled.connect(toggled)
        self.header.toggled.connect(foldout.animate)
        layout.addWidget(foldout)

    def refresh_sidebar_theme(self):
        color = sidebar_colors()['muted']
        self.icon = tool_icon(self.glyph, color)
        self.header.setIcon(self.icon)
        self.arrow.setProperty('chevronColor', color)
        self.arrow.update()
        for index in range(self.items.count()):
            self.items.item(index).setIcon(self.icon)

    def set_entries(self, entries):
        if entries == self.entries:
            return
        self.entries = entries
        scroll = self.items.verticalScrollBar()
        position = scroll.value()
        self.items.clear()
        for entry in entries:
            item = QListWidgetItem(self.icon,entry.name,self.items)
            item.setToolTip(f'{entry.name}\n{entry.source}')
        self.fit_items()
        self.items.setVisible(bool(entries))
        self.empty.setVisible(not entries)
        self.items.doItemsLayout()
        scroll.setValue(position)

    def fit_items(self):
        count = min(4, self.items.count())
        height = sum(max(25, self.items.sizeHintForRow(row)) for row in range(count))
        self.items.setFixedHeight(height + 2 * self.items.frameWidth())


class CodexQuota(QWidget):
    def __init__(self, codex):
        super().__init__()
        self.setObjectName('workQuota')
        self.codex = codex
        self.account = None
        self.snapshot = {}
        self.pending = None
        self.updates_during_read = {}
        self.error = ''
        self.rows = {}
        column = QVBoxLayout(self)
        column.setContentsMargins(2,6,2,3)
        column.setSpacing(3)
        self.title = QLabel('Codex 额度')
        self.title.setObjectName('workQuotaTitle')
        column.addWidget(self.title)
        for minutes,name in ((300,'5 小时'),(10080,'一周')):
            row = QHBoxLayout()
            row.setSpacing(6)
            label = QLabel(name)
            label.setObjectName('workQuotaTitle')
            label.setFixedWidth(35)
            bar = QProgressBar()
            bar.setObjectName('workQuotaBar')
            bar.setAccessibleName(name + '额度剩余')
            bar.setRange(0,1000)
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            value = QLabel()
            value.setObjectName('workQuotaValue')
            value.setFixedWidth(42)
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(label)
            row.addWidget(bar,1)
            row.addWidget(value)
            column.addLayout(row)
            self.rows[minutes] = (bar,value)
        self.poll = QTimer(self,interval=60000)
        self.poll.timeout.connect(self.refresh)
        codex.account_changed.connect(self.account_changed)
        codex.event.connect(self.receive)
        self.account_changed()

    def showEvent(self, event):
        super().showEvent(event)
        self.poll.start()
        self.refresh()

    def hideEvent(self, event):
        self.poll.stop()
        super().hideEvent(event)

    def account_changed(self):
        account = self.codex.account if self.codex.signed_in else None
        if account != self.account:
            self.account = account
            self.snapshot = {}
            self.pending = None
            self.error = ''
            if self.isVisible():
                self.refresh()
        self.render()

    @staticmethod
    def bucket(payload):
        buckets = payload.get('rateLimitsByLimitId') or {}
        snapshot = buckets.get('codex') or payload.get('rateLimits') or {}
        return snapshot if snapshot.get('limitId') in (None,'codex') else {}

    @staticmethod
    def merge(snapshot, update):
        result = {}
        for slot in ('primary','secondary'):
            previous = snapshot.get(slot) or {}
            latest = update.get(slot) or {}
            result[slot] = {**previous, **{key:value for key,value in latest.items() if value is not None}}
        return result

    def refresh(self):
        if not self.account or not self.codex.connected or self.pending is not None:
            return
        token = self.pending = object()
        self.updates_during_read = {}
        self.codex.call('account/rateLimits/read',{},lambda result,error: self.received(token,result,error),
                        disconnect_on_timeout=False)

    def received(self, token, result, error):
        if token is not self.pending:
            return
        self.pending = None
        self.error = error if not self.updates_during_read else ''
        if not error:
            self.snapshot = self.merge(self.bucket(result),self.updates_during_read)
        self.render()

    def receive(self, method, params):
        if method != 'account/rateLimits/updated' or not self.account:
            return
        update = self.bucket(params)
        if not any(update.get(slot) for slot in ('primary','secondary')):
            return
        self.snapshot = self.merge(self.snapshot,update)
        if self.pending is not None:
            self.updates_during_read = self.merge(self.updates_during_read,update)
        self.error = ''
        self.render()

    def render(self):
        by_duration = {}
        for window in self.snapshot.values():
            used = window.get('usedPercent')
            if isinstance(used,(int,float)) and isfinite(used):
                by_duration[window.get('windowDurationMins')] = max(0,min(100,100-used))
        for minutes,(bar,value) in self.rows.items():
            remaining = by_duration.get(minutes)
            if remaining is None:
                bar.reset()
                value.setText('—')
            else:
                bar.setValue(round(remaining*10))
                value.setText(f'{round(remaining,1):g}%')
        self.title.setText('Codex 额度 · 更新失败' if self.error else 'Codex 额度')
        status = 'ChatGPT 登录后显示额度' if not self.account else self.error or '显示额度剩余百分比'
        self.setToolTip(status)


class PicturePreview(QPushButton):
    double_clicked = Signal()

    def __init__(self):
        super().__init__()
        self.picture = QPixmap()
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_image(self, image):
        self.picture = QPixmap.fromImage(image)
        if not image.isNull():
            size = image.size().scaled(QSize(114, 66), Qt.AspectRatioMode.KeepAspectRatio)
            self.setFixedSize(size.expandedTo(QSize(22, 22)))
        self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            self.double_clicked.emit()
        else:
            super().mouseDoubleClickEvent(event)

    def paintEvent(self, event):
        if self.picture.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        source = self.picture.deviceIndependentSize()
        scale = min(self.width() / source.width(), self.height() / source.height())
        target = QRectF(0, 0, source.width() * scale, source.height() * scale)
        target.moveCenter(QRectF(self.rect()).center())
        brush = QBrush(self.picture)
        brush.setTransform(QTransform().translate(target.x(), target.y()).scale(scale, scale))
        painter.setBrush(brush)
        painter.setPen(QPen(QColor(COLORS['gray']), 1) if self.hasFocus() else Qt.PenStyle.NoPen)
        painter.drawRoundedRect(target, 6, 6)


class ProjectSidebar(QFrame):
    folders_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName('workSidebar')
        self._ronova_fixed_theme = True
        self.refresh_sidebar_theme()
        self.setAcceptDrops(True)

    def refresh_sidebar_theme(self):
        self.setStyleSheet(Template('''
            QFrame#workSidebar { background:$background; border:1px solid $border; border-radius:20px; }
            QFrame#workSidebar[folderDropActive="true"] { background:$selection; border-color:$gray; }
            QFrame#workLibrary { background:$surface; border:1px solid $border; border-radius:12px; }
            QPushButton#workLibraryHeader { color:$text; background:transparent; text-align:left; padding:5px 9px; }
            QPushButton#workLibraryHeader:hover { background:$selection; }
            QListWidget#workLibraryItems { background:transparent; color:$text; border:0; outline:0; font-size:13px; }
            QListWidget#workLibraryItems::item { color:$text; min-height:25px; padding-left:5px; }
            QTreeWidget { background:transparent; color:$text; border:0; outline:0; }
            QTreeWidget::item { color:$text; min-height:30px; padding:2px 5px; }
            QTreeWidget::item:selected { background:transparent; color:$text; }
            QLabel#workSection, QLabel#workHint, QLabel#workQuotaTitle { color:$muted; }
            QLabel#workQuotaValue { color:$text; }
            QPushButton#workNewChat { background:$surface; color:$text; text-align:left; }
            QPushButton#workNewChat:hover, QToolButton#workChatMenu:hover { background:$selection; }
            QPushButton#workAddProject, QToolButton#workChatMenu { color:$muted; background:transparent; }
            QPushButton#workAddProject:hover { background:$selection; }
            QWidget#workQuota { border-top:1px solid $border; }
            QProgressBar#workQuotaBar { background:$border; border:0; border-radius:3px; }
            QProgressBar#workQuotaBar::chunk { background:$primary; border-radius:3px; }
        ''').substitute(sidebar_colors()))

    @staticmethod
    def folders(mime):
        paths = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            try:
                if path.is_absolute() and path.is_dir():
                    paths.append(path)
            except OSError:
                continue
        return list(dict.fromkeys(paths))

    @staticmethod
    def accept_folder_drop(event):
        for action in (Qt.DropAction.LinkAction,Qt.DropAction.CopyAction):
            if event.possibleActions() & action:
                event.setDropAction(action)
                event.accept()
                return True
        event.ignore()
        return False

    def highlight_drop(self, active):
        if bool(self.property('folderDropActive')) != active:
            self.setProperty('folderDropActive',active)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    def dragEnterEvent(self, event):
        if self.folders(event.mimeData()):
            self.highlight_drop(self.accept_folder_drop(event))
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.accept_folder_drop(event)

    def dragLeaveEvent(self, event):
        self.highlight_drop(False)
        event.accept()

    def dropEvent(self, event):
        self.highlight_drop(False)
        folders = self.folders(event.mimeData())
        if folders and self.accept_folder_drop(event):
            self.folders_dropped.emit(folders)
        else:
            event.ignore()


class WorkspaceTree(QTreeWidget):
    def paint_project_arrow(self, painter, rect, index):
        x = rect.left() - self.indentation()/2
        y = QRectF(rect).center().y()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(sidebar_colors()['muted']),1.25,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
        if self.isExpanded(index):
            painter.drawPolyline([QPointF(x-3,y-1),QPointF(x,y+2),QPointF(x+3,y-1)])
        else:
            painter.drawPolyline([QPointF(x-1,y-3),QPointF(x+2,y),QPointF(x-1,y+3)])
        painter.restore()

    def drawRow(self, painter, option, index):
        text_rect = self.visualRect(index)
        state = QStyle.StateFlag
        selected = self.selectionModel().isSelected(index.siblingAtColumn(0))
        hovered = bool(option.state & state.State_MouseOver)
        if selected or hovered:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(sidebar_colors()['selection' if selected else 'elevated']))
            highlight = QRectF(option.rect).adjusted(1,2,-1,-2)
            highlight.setLeft(text_rect.left())
            painter.drawRoundedRect(highlight,9,9)
            painter.restore()
        item = QStyleOptionViewItem(option)
        item.state &= ~(state.State_Selected | state.State_MouseOver | state.State_HasFocus)
        item.rect = text_rect
        self.itemDelegateForIndex(index).paint(painter,item,index)
        if not index.parent().isValid() and self.model().hasChildren(index):
            self.paint_project_arrow(painter,text_rect,index)


class MenuButton(QToolButton):
    def __init__(self, name, accessible, populate):
        super().__init__()
        self.setObjectName(name)
        self.setAccessibleName(accessible)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        menu = QMenu(self)
        menu.aboutToShow.connect(lambda: self.fill(menu,populate))
        self.setMenu(menu)

    @staticmethod
    def fill(menu, populate):
        menu.clear()
        populate(menu)


def mount_panel(menu, panel):
    menu.setObjectName('workControlMenu')
    action = QWidgetAction(menu)
    action.setDefaultWidget(panel)
    menu.addAction(action)


class PermissionPicker(QWidget):
    def __init__(self, permissions, selected, icons, choose, dismiss):
        super().__init__()
        self.setObjectName('workPermissionPicker')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setMinimumWidth(306)
        content = QVBoxLayout(self)
        content.setContentsMargins(8,7,8,7)
        content.setSpacing(2)
        header = QHBoxLayout()
        header.setContentsMargins(6,0,4,4)
        title = QLabel('应如何批准 ChatGPT 操作？')
        title.setObjectName('workPopupHint')
        header.addWidget(title,1)
        more = QPushButton('了解更多')
        more.setObjectName('workPermissionHelp')
        more.clicked.connect(self.open_help)
        header.addWidget(more)
        content.addLayout(header)
        for mode,(title,description) in permissions.items():
            row = QPushButton()
            row.setObjectName('workPermissionRow')
            row.setProperty('permission',mode)
            row.setProperty('chosen',mode == selected)
            row.setAccessibleName(title)
            row.setAccessibleDescription(description)
            row.setMinimumHeight(48)
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            line = QHBoxLayout(row)
            line.setContentsMargins(8,6,8,6)
            line.setSpacing(9)
            icon = QLabel()
            icon.setPixmap(icons[mode].pixmap(QSize(17,17)))
            icon.setFixedWidth(18)
            line.addWidget(icon)
            words = QVBoxLayout()
            words.setSpacing(2)
            name = QLabel(title)
            name.setObjectName('workPermissionName')
            name.setFont(emphasis_font())
            hint = QLabel(description)
            hint.setObjectName('workPopupHint')
            words.addWidget(name)
            words.addWidget(hint)
            line.addLayout(words,1)
            check = QLabel('✓' if mode == selected else '')
            check.setObjectName('workPermissionCheck')
            check.setFixedWidth(16)
            line.addWidget(check)
            for label in (icon,name,hint,check):
                label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row.clicked.connect(lambda _checked,key=mode: self.select(key,choose,dismiss))
            content.addWidget(row)

    @staticmethod
    def select(mode, choose, dismiss):
        choose(mode)
        dismiss()

    @staticmethod
    def open_help():
        QDesktopServices.openUrl(QUrl('https://learn.chatgpt.com/docs/permissions'))


class EffortSlider(QSlider):
    INSET = 14

    def __init__(self):
        super().__init__(Qt.Orientation.Horizontal)
        self.setObjectName('workEffortSlider')
        self.setAccessibleName('推理强度')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.valueChanged.connect(self.update)
        self.rangeChanged.connect(self.update)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        middle = self.height() / 2
        steps = self.maximum() - self.minimum()
        travel = max(1,self.width() - 2 * self.INSET)
        offset = self.value() - self.minimum()
        position = self.INSET + (travel * offset / steps if steps else 0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS['border']))
        painter.drawRoundedRect(QRectF(1,middle-11,self.width()-2,22),11,11)
        painter.setBrush(QColor(COLORS['primary']))
        painter.drawRoundedRect(QRectF(1,middle-11,position + 11,22),11,11)
        for index in range(steps + 1):
            x = self.INSET + (travel * index / steps if steps else 0)
            painter.setBrush(QColor(COLORS['elevated'] if index <= offset else COLORS['disabled']))
            painter.drawEllipse(QRectF(x-1.8,middle-1.8,3.6,3.6))
        painter.setPen(QPen(QColor(COLORS['gray'] if self.hasFocus() else COLORS['border']),1))
        painter.setBrush(QColor(COLORS['surface']))
        painter.drawEllipse(QRectF(position-13,middle-13,26,26))

    def seek(self, event):
        x = round(event.position().x() - self.INSET)
        span = max(1,self.width() - 2 * self.INSET)
        self.setValue(QStyle.sliderValueFromPosition(self.minimum(),self.maximum(),x,span))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.setSliderDown(True)
        self.seek(event)
        event.accept()

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.seek(event)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.seek(event)
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


# Unranked models retain the relative order supplied by Codex.
CODEX_MODEL_ORDER = {
    'gpt-6.2-astra': 0,
    'gpt-6.2-sol': 1,
    'gpt-6-astra': 2,
    'gpt-6.1-sol': 3,
    'gpt-6-sol': 4,
    'gpt-6.1-terra': 5,
    'gpt-6-luna': 6,
}


class ModelPicker(QWidget):
    def __init__(self, catalog, selected, effort, effort_names, choose, available_geometry):
        super().__init__()
        self.setObjectName('workModelPicker')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setMinimumWidth(268)
        self.available_geometry = available_geometry
        self.catalog = catalog
        self.effort_names = effort_names
        self.choose = choose
        self.selected = selected if selected in catalog else next(iter(catalog))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9,7,9,6)
        layout.setSpacing(3)
        self.heading = QWidget()
        heading = QHBoxLayout(self.heading)
        heading.setContentsMargins(0,0,0,0)
        hint = QLabel('推理强度')
        hint.setObjectName('workPopupHint')
        heading.addWidget(hint,1)
        self.effort_label = QLabel()
        self.effort_label.setObjectName('workCurrentEffort')
        self.effort_label.setFont(emphasis_font())
        self.effort_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(self.effort_label,1)
        self.reset = QToolButton()
        self.reset.setObjectName('workEffortReset')
        self.reset.setIcon(tool_icon('<path d="M4 11a8 8 0 1 1 2 6M4 5v6h6"/>',COLORS['muted']))
        self.reset.setIconSize(QSize(18,18))
        self.reset.setAccessibleName('恢复模型默认推理强度')
        self.reset.setToolTip('恢复模型默认推理强度')
        self.reset.setFixedSize(26,26)
        self.reset.clicked.connect(self.reset_effort)
        reset_area = QHBoxLayout()
        reset_area.addStretch()
        reset_area.addWidget(self.reset)
        heading.addLayout(reset_area,1)
        layout.addWidget(self.heading)

        self.model_button = QToolButton()
        self.model_button.setObjectName('workModelChoice')
        self.model_button.setAccessibleName('选择 Codex 模型')
        self.model_button.setMaximumWidth(available_geometry.width() - 36)
        self.model_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.model_menu = QMenu(self.model_button)
        self.model_button.setMenu(self.model_menu)
        layout.addWidget(self.model_button,0,Qt.AlignmentFlag.AlignHCenter)
        self.models = QListWidget()
        self.models.setObjectName('workModelList')
        self.models.setAccessibleName('可用 Codex 模型')
        self.models.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.models.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        metrics = self.models.fontMetrics()
        row_height = max(28, metrics.height() + 12)
        width = 0
        ordered = sorted(catalog, key=lambda slug: CODEX_MODEL_ORDER.get(slug, len(CODEX_MODEL_ORDER)))
        for slug in ordered:
            name = catalog[slug].get('displayName') or slug
            item = QListWidgetItem(name, self.models)
            item.setData(Qt.ItemDataRole.UserRole, slug)
            item.setToolTip(name)
            row_width = metrics.horizontalAdvance(name) + 22
            item.setSizeHint(QSize(row_width, row_height))
            width = max(width, row_width)
            if slug == self.selected:
                self.selected_item = item
        scrollbar = self.models.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
        self.models.setMinimumWidth(min(width + scrollbar + 4, available_geometry.width() - 36))
        height = min(len(catalog) * row_height + 4, max(row_height, available_geometry.height() - 160))
        self.models.setFixedHeight(height)
        self.models.itemClicked.connect(self.select_model)
        self.models.itemActivated.connect(self.select_model)
        mount_panel(self.model_menu, self.models)
        self.model_menu.setObjectName('workModelMenu')
        self.model_menu.aboutToShow.connect(self.focus_model_list)
        self.slider = EffortSlider()
        layout.addWidget(self.slider)
        self.slider.valueChanged.connect(self.effort_changed)
        self.sync_effort(effort if self.selected == selected else None)
        self.sync_model_button()

    def sync_effort(self, effort=None):
        model = self.catalog[self.selected]
        self.efforts = [entry['reasoningEffort'] for entry in model.get('supportedReasoningEfforts') or []]
        default = model.get('defaultReasoningEffort')
        self.default_index = self.efforts.index(default) if default in self.efforts else 0
        index = self.efforts.index(effort) if effort in self.efforts else self.default_index
        with QSignalBlocker(self.slider):
            self.slider.setRange(0,max(0,len(self.efforts)-1))
            self.slider.setValue(index)
        self.slider.setEnabled(len(self.efforts) > 1)
        self.show_effort(index)

    def focus_model_list(self):
        QTimer.singleShot(0, self.models, self.activate_model_list)

    def activate_model_list(self):
        if self.model_menu.isVisible():
            self.models.setCurrentItem(self.selected_item)
            self.models.setFocus(Qt.FocusReason.PopupFocusReason)

    def sync_model_button(self):
        name = self.catalog[self.selected].get('displayName') or self.selected
        self.model_button.setText(name + '  ▾')
        self.model_button.setToolTip(name)
        self.slider.setVisible(bool(self.efforts))

    def fit_menu(self):
        menu = self.parentWidget()
        if not isinstance(menu, QMenu) or not menu.isVisible():
            return
        self.updateGeometry()
        menu.adjustSize()
        bounds = self.available_geometry
        x = max(bounds.left(), min(menu.x(), bounds.right() - menu.width() + 1))
        y = max(bounds.top(), min(menu.y(), bounds.bottom() - menu.height() + 1))
        menu.move(x, y)

    def show_effort(self, index):
        value = self.efforts[index] if self.efforts else None
        self.effort_label.setText(self.effort_names.get(value,value) if value else '默认')
        self.slider.setAccessibleDescription(self.effort_label.text())
        self.reset.setEnabled(bool(self.efforts) and index != self.default_index)
        self.slider.update()

    def select_model(self, item):
        self.model_menu.close()
        model = item.data(Qt.ItemDataRole.UserRole)
        if model == self.selected:
            return
        self.selected = model
        self.selected_item = item
        self.sync_effort()
        self.sync_model_button()
        effort = self.efforts[self.slider.value()] if self.efforts else None
        self.choose(model, effort)
        self.fit_menu()

    def effort_changed(self, index):
        if self.efforts:
            self.show_effort(index)
            self.choose(self.selected,self.efforts[index])

    def reset_effort(self):
        self.slider.setValue(self.default_index)


def usage_percent(used, limit):
    return f'{round(used * 100 / limit,1):g}%'


class ContextButton(MenuButton):
    def __init__(self, populate):
        super().__init__('workContext','上下文容量',populate)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setFixedSize(28,30)
        self.setIconSize(QSize(14,14))
        self.usage = None

    def show_usage(self, usage):
        if usage == self.usage and not self.icon().isNull():
            return
        self.usage = usage
        hint = '发送消息后显示上下文用量'
        if usage is not None:
            used, limit = usage
            hint = f'已占用 {usage_percent(used,limit)}' if limit else f'已用 {used:,} tokens'
        self.setToolTip(hint)
        icon = QPixmap(32,32)
        icon.setDevicePixelRatio(2)
        icon.fill(Qt.GlobalColor.transparent)
        painter = QPainter(icon)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(2.5,2.5,11,11)
        painter.setPen(QPen(QColor(COLORS['border']),2.2))
        painter.drawEllipse(rect)
        if usage is not None:
            used, limit = usage
            if limit:
                painter.setPen(QPen(QColor(COLORS['primary']),2.2,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap))
                painter.drawArc(rect,90*16,-round(360*16*min(1,used/limit)))
        painter.end()
        self.setIcon(QIcon(icon))
