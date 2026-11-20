"""Display preview images explicitly produced during a Codex turn."""
import os
from pathlib import Path, PureWindowsPath
import re
from urllib.parse import unquote

from PySide6.QtCore import QEasingCurve, QRect, QSize, Qt, QUrl, Signal, QVariantAnimation
from PySide6.QtGui import QImage, QImageReader, QPainter
from PySide6.QtWidgets import QFrame


IMAGE_TYPES = frozenset('.png .jpg .jpeg .bmp .gif .webp .ico .tif .tiff'.split())
PREVIEW_NAME = re.compile(r'preview|thumbnail|预览|縮略圖|缩略图',re.IGNORECASE)
MARKDOWN_LINK = re.compile(r'\[([^\]\r\n]*)\]\(\s*(?:<([^>\r\n]+)>|([^\s)]+))\s*\)')
CODE_PATH = re.compile(r'\x60([^\x60\r\n]+)\x60')
PREVIEW_LINE = re.compile(r'^\s*(?:[-*]\s*)?(?:已(?:生成|制作|导出|渲染|输出)了?\s*)?'
                          r'\*{0,2}(?:预览图|缩略图|preview(?: image)?)\*{0,2}(?=\s|[:：])\s*[:：]?',
                          re.IGNORECASE)


def image_file(value, root, *, labeled=False):
    if not isinstance(value,str) or not value or len(value)>4096 or any(ord(char)<32 for char in value):
        return None
    if value.startswith('file:'):
        url = QUrl(value)
        if not url.isLocalFile() or url.host():
            return None
        value = url.toLocalFile()
    else:
        value = unquote(value.removeprefix('sandbox:'))
    if re.match(r'^/[A-Za-z]:[/\\]',value):
        value = value[1:]
    windows = PureWindowsPath(value)
    if windows.suffix.lower() not in IMAGE_TYPES or (not labeled and not PREVIEW_NAME.search(windows.stem)):
        return None
    if windows.drive.startswith('\\') or ':' in value[len(windows.drive):] or value.startswith('//'):
        return None
    path = root / value
    try:
        if not Path(os.path.abspath(path)).is_relative_to(root):
            return None
        path = path.resolve()
        if path.is_relative_to(root) and path.suffix.lower() in IMAGE_TYPES and path.is_file():
            return path
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def preview_candidates(turn):
    items = tuple(reversed(tuple(turn.items.values())))
    for item in items:
        if item.get('type') != 'agentMessage' or item.get('phase') == 'commentary':
            continue
        for line in (item.get('text') or '').splitlines():
            prefix = PREVIEW_LINE.match(line)
            for label, first, second in MARKDOWN_LINK.findall(line):
                yield first or second, bool(prefix or PREVIEW_NAME.search(label))
            for value in CODE_PATH.findall(line):
                yield value, bool(prefix)
            if len(line) < 260:
                yield (line[prefix.end():] if prefix else line).strip(' \t-*'), bool(prefix)
    for item in items:
        if item.get('type') == 'fileChange' and item.get('status') == 'completed':
            for change in item.get('changes',[]):
                kind = change.get('kind') or {}
                if kind.get('type') in ('add','update'):
                    yield kind.get('move_path') or kind.get('movePath') or change.get('path'),False


def read_image(path):
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and max(size.width(),size.height()) > 4096:
        reader.setScaledSize(size.scaled(QSize(4096,4096),Qt.AspectRatioMode.KeepAspectRatio))
    return reader.read()


def latest_preview(turns, cwd):
    turn = next((turn for turn in reversed(turns) if turn.state == 'done'),None)
    if turn is None:
        return None,QImage()
    try:
        root = cwd.resolve()
    except (OSError, RuntimeError):
        return None,QImage()
    for value,labeled in preview_candidates(turn):
        if path := image_file(value,root,labeled=labeled):
            image = read_image(path)
            if not image.isNull():
                return path,image
    return None,QImage()


class WorkspacePreview(QFrame):
    edit_requested = Signal()

    def __init__(self):
        super().__init__()
        self.image = QImage()
        self.previous_image = QImage()
        self.image_transition = QVariantAnimation(self,startValue=0.,endValue=1.,duration=160)
        self.image_transition.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.image_transition.valueChanged.connect(lambda _progress: self.update())
        self.image_transition.finished.connect(self.finish_image_transition)
        self.setObjectName('workPreview')
        self.setAccessibleName('编辑预览图')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)

    def set_chat(self, chat, cwd):
        path,image = latest_preview(chat.turns,cwd) if chat is not None else (None,QImage())
        self.set_image(path,image)

    def open_link(self, url, cwd):
        try:
            root = cwd.resolve()
        except (OSError,RuntimeError):
            return False
        value = url.toString(QUrl.ComponentFormattingOption.FullyEncoded)
        path = image_file(value,root,labeled=True)
        if path is None:
            return False
        image = read_image(path)
        if image.isNull():
            return False
        self.set_image(path,image)
        return True

    def set_image(self, path, image):
        replace_visible_image = (self.isVisible() and not self.image.isNull() and not image.isNull()
                                 and image != self.image)
        self.image_transition.stop()
        self.previous_image = self.image if replace_visible_image else QImage()
        self.image = image
        self.setToolTip(f'{path.name}\n双击标记预览图' if path is not None else '')
        if replace_visible_image:
            self.image_transition.start()
        self.update()

    def finish_image_transition(self):
        self.previous_image = QImage()
        self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.image.isNull():
            self.edit_requested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter,Qt.Key.Key_Space) and not self.image.isNull():
            self.edit_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        area = self.rect().adjusted(7,7,-7,-7)
        if self.image.isNull() or area.isEmpty():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if not self.previous_image.isNull():
            progress = self.image_transition.currentValue()
            painter.setOpacity(1.-progress)
            self.draw_image(painter,area,self.previous_image)
            painter.setOpacity(progress)
        self.draw_image(painter,area,self.image)

    @staticmethod
    def draw_image(painter, area, image):
        size = image.size().scaled(area.size(),Qt.AspectRatioMode.KeepAspectRatio)
        target = QRect(area.topLeft(),size)
        target.moveCenter(area.center())
        painter.drawImage(target,image)
