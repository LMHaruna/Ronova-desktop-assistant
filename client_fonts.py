"""Typography shared by Qt widgets, chat documents and painted controls."""
import os
import re
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget


ROOT = Path(__file__).resolve().parent
UI_FONT_SIZE = 14
CODE_FONT_SIZE = 13
MISANS_PATH = Path('design/fonts/MiSansVF.ttf')
MISANS_FAMILIES = ('MiSans VF', 'MiSans')

WINDOWS_FONTS = {
    'Segoe UI': ('segoeui.ttf', 'seguisb.ttf', 'segoeuib.ttf', 'segoeuii.ttf', 'segoeuiz.ttf'),
    'Microsoft YaHei UI': ('msyh.ttc', 'msyhbd.ttc'),
    'Consolas': ('consola.ttf', 'consolab.ttf', 'consolai.ttf', 'consolaz.ttf'),
}


class Typography(QObject):
    changed = Signal(int)

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.scale = 100
        self.updating = False
        self.base_font = None
        app.installEventFilter(self)

    def stylesheet(self, text):
        return re.sub(r'(font-size\s*:\s*)([0-9]+(?:\.[0-9]+)?)(px|pt)',
                      lambda match: match.group(1) + str(max(1, round(float(match.group(2)) * self.scale / 100))) + match.group(3),
                      text, flags=re.IGNORECASE)

    def apply_style(self, widget):
        current = widget.styleSheet()
        applied = widget.property('_ronova_scaled_style')
        if current != applied:
            widget.setProperty('_ronova_base_style', current)
        original = widget.property('_ronova_base_style') or ''
        updated = self.stylesheet(original)
        if current != updated:
            widget.setProperty('_ronova_scaled_style', updated)
            widget.setStyleSheet(updated)
        else:
            widget.setProperty('_ronova_scaled_style', current)

    def eventFilter(self, obj, event):
        if (not self.updating and event.type() == QEvent.Type.StyleChange and isinstance(obj, QWidget)
                and (obj.styleSheet() or obj.property('_ronova_base_style') is not None)):
            self.updating = True
            try:
                self.apply_style(obj)
            finally:
                self.updating = False
        return False

    def set_scale(self, scale):
        if self.base_font is None:
            return
        scale = int(scale)
        target = QFont(self.base_font)
        target.setPixelSize(max(1, round(UI_FONT_SIZE * scale / 100)))
        if self.scale == scale and self.app.font() == target:
            return
        self.scale = scale
        self.updating = True
        try:
            font = QFont(self.base_font)
            font.setPixelSize(scaled_size(UI_FONT_SIZE))
            self.app.setFont(font)
            for widget in self.app.allWidgets():
                if widget.styleSheet() or widget.property('_ronova_base_style') is not None:
                    self.apply_style(widget)
                if widget.property('_ronova_code_font'):
                    widget.setFont(code_font())
        finally:
            self.updating = False
        self.changed.emit(scale)


_manager = None


def typography():
    global _manager
    app = QApplication.instance()
    if app is None:
        return None
    if _manager is None or _manager.app is not app:
        _manager = Typography(app)
    return _manager


def scaled_size(size):
    manager = typography()
    return max(1, round(size * (manager.scale if manager else 100) / 100))


def misans_family(asset_root=ROOT):
    families = [name for name in MISANS_FAMILIES if QFontDatabase.hasFamily(name)]
    if not families:
        path = Path(asset_root) / MISANS_PATH
        if path.is_file():
            installed = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(str(path)))
            families = [name for name in MISANS_FAMILIES if name in installed]
    return next(iter(families), None)


def desktop_icon_font():
    # Keep desktop icon names at the platform point size regardless of UI scaling.
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    if family := misans_family():
        font.setFamily(family)
        font.setStyleName('Regular' if 'Regular' in QFontDatabase.styles(family) else '')
    return font


def load_fonts(asset_root=ROOT, scale=None):
    app = QGuiApplication.instance()
    if os.name == 'nt':
        system_fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
        # The Windows offscreen platform may start with an empty font database.
        for family, files in WINDOWS_FONTS.items():
            if not QFontDatabase.hasFamily(family):
                for name in files:
                    path = system_fonts / name
                    if path.is_file():
                        QFontDatabase.addApplicationFont(str(path))

    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    families = [name for name in ('Segoe UI', 'Microsoft YaHei UI')
                if QFontDatabase.hasFamily(name)]
    if not families:
        families = font.families()
        if not QFontDatabase.hasFamily(font.family()) and (family := misans_family(asset_root)):
            families = [family]
    if families:
        font.setFamilies(families)
    font.setStyleName('')
    font.setWeight(QFont.Weight.Normal)
    font.setPixelSize(UI_FONT_SIZE)
    manager = typography()
    manager.base_font = QFont(font)
    manager.set_scale(manager.scale if scale is None else scale)
    return font.family()


def emphasis_font():
    font = QFont()
    font.setWeight(QFont.Weight.DemiBold)
    return font


def code_font():
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    if QFontDatabase.hasFamily('Consolas'):
        font.setFamilies(['Consolas', 'Microsoft YaHei UI'])
    font.setStyleName('')
    font.setPixelSize(scaled_size(CODE_FONT_SIZE))
    return font
