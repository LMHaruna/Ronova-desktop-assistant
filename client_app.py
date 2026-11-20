"""Windows companion: idle incoming calls and explicit-answer voice capture."""
from pathlib import Path
import argparse
from hashlib import sha256
import json
import math
import os
import sys
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'runtime/client-tools'))
from PySide6.QtCore import QEvent, QLockFile, QPoint, QRectF, QTimer, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication,QLabel,QMenu,QMessageBox,QPushButton,QSystemTrayIcon,QToolTip,QWidget
from client_controller import DesktopController
from client_fonts import UI_FONT_SIZE, emphasis_font, load_fonts, scaled_size
from client_scene import PHONE_BODY, PHONE_SCREEN, SceneAssetError, SceneAssets, phone_clock
from client_panels import COMPACT_CHAT_CSS, PhoneOverlay, phone_css
from client_motion import HeartButton, MotionButton, SpeakingMotion
from client_affinity import AffinityBar
from client_shell import (CloseButton, DesktopLauncher, FloatingPanel, PanelTransitions, WINDOW_FLAGS,
                          WindowDrag, available_at, constrain, show_window)
from client_theme import COLORS, recolor_style, set_theme, themed

PHONE_WINDOW_WIDTH = PHONE_BODY[2] + 34


def stylesheet(colors):
    return f'''QDialog,QWidget{{background:{colors['surface']};color:{colors['text']}}}
        QPushButton{{background:{colors['elevated']};border:1px solid {colors['border']};border-radius:10px;padding:9px 14px}}
        QPushButton:hover{{border-color:{colors['gray']}}} QPushButton:disabled{{color:{colors['disabled']}}}
        QPlainTextEdit,QTextBrowser,QComboBox{{background:{colors['background']};border:1px solid {colors['border']};border-radius:8px;padding:8px}}
        QMenu{{background:{colors['surface']};color:{colors['text']};border:1px solid {colors['border']}}}
        QMenu::item:selected{{background:{colors['selection']}}}
        QTabWidget::pane{{border:0}} QTabBar::tab{{padding:12px 18px;background:{colors['elevated']}}}
        QTabBar::tab:selected{{background:{colors['selection']}}} QSlider::groove:horizontal{{height:5px;background:{colors['border']}}}
        QSlider::sub-page:horizontal{{background:{colors['primary']}}}
        QSlider::handle:horizontal{{width:16px;margin:-6px 0;border-radius:8px;background:{colors['surface']};border:1px solid {colors['primary']}}}'''


class ClientWindow(QWidget):
    def __init__(self,controller,asset_root=ROOT, *, desktop=False):
        set_theme(controller.preferences.theme)
        load_fonts(controller.preferences.font_scale)
        super().__init__()
        self.controller = controller
        self._theme = controller.preferences.theme
        self.desktop = desktop
        self.asset_root = Path(asset_root)
        self.codex = None
        if desktop:
            from client_codex import CodexAppServer
            from client_credentials import user_credentials_path
            home = (user_credentials_path().parent/'Codex' if controller.root.resolve()==ROOT
                    else controller.root/'data'/'codex')
            self.codex = CodexAppServer(home,self)
        self.video_call = False
        self.voice_start = None
        self.phone_submit = None
        self.launcher = self.settings_window = self.workspace_window = self.desktop_organizer = None
        self.workspace = None
        self.workspace_voice = None
        self.capture_session = self.capture_focus = self.capture_audio = None
        self.capture_windows = ()
        self.capture_opening_workspace = False
        self.phone_positioned = False
        self.assets = SceneAssets(asset_root)
        self.colors = COLORS
        self.setStyleSheet(stylesheet(self.colors))
        self.setWindowTitle('若娜瓦 · RONOVA')
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if desktop:
            self.setWindowTitle('陪伴系统 · 若娜瓦')
            self.setObjectName('companionWindow')
            self.setStyleSheet(stylesheet(self.colors) + 'QWidget#companionWindow{background:transparent;}')
            self.setWindowFlags(WINDOW_FLAGS)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.resize(PHONE_WINDOW_WIDTH,PHONE_BODY[3])
            self.setMinimumSize(248,460)
        else:
            self.resize(1280,802); self.setMinimumSize(960,602)
            self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.state = self.controller.snapshot()
        self.screen = 'home'
        self.settings_origin = 'home'
        self.assets.set_stage(self.controller.memory.profile()['stage'])
        self._oath_unlock_notified=self.controller.memory.profile()['affinity']==100
        self._oath_unlock_pending=False
        self.call_started = None
        self.speaking_motion = SpeakingMotion()
        self._scene_cache = None
        self._scene_cache_signature = None; self._scene_variants = {}
        self._portrait_transition_from=None
        self._portrait_transition_started=0.0
        self._portrait_transition_duration=.3
        self._display_clock = None; self._display_second = None
        self.scale, self.offset_x,self.offset_y = 1,0,0
        self.settings_button = self.button('设置',(1160,12,86,40),self.settings)
        self.voice_button = self.button('语音通话',(836,660,60,60),self.voice)
        self.end_button = self.button('结束通话',(940,660,60,60),self.end_or_decline)
        self.text_button = self.button('文字聊天',(1044,660,60,60),self.text_or_later)
        self.status_button = self.button('停止回应',(879,478,182,61),self.status_action)
        self.oath_dialog=None
        if not desktop:
            self.oath_button=HeartButton('誓约',self)
            self.oath_button.base_geometry=(700,665,68,64)
            self.oath_button.setObjectName('oathHomeButton')
            self.oath_button.clicked.connect(self.open_oath)
        self.affinity_bar=AffinityBar(self)
        self.affinity_bar.setValue(self.controller.memory.profile()['affinity'])
        self.overlay = PhoneOverlay(self)
        self.text_pane = self.overlay.surface.text
        self.settings_pane = self.overlay.surface.settings
        if desktop:
            self.oath_button = self.text_pane.call_options.oath
            self.setup_assistant(Path(asset_root))
        self.sync_oath_entry()
        self.tray = None; self._quitting = False
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(QIcon(self.assets.avatar),self)
            self.tray.setToolTip('若娜瓦 · 等待来电')
            menu = QMenu(self)
            actions = [('打开若娜瓦',self.restore_window),('测试来电',lambda:self.safe(controller.test_incoming_call))]
            if desktop:
                actions[1:1] = [('隐藏头像',self.launcher.hide),('陪伴系统',self.open_companion),('设置',self.settings)]
            for title,callback in (*actions,('退出',self.quit_client)):
                item = QAction(title,menu); item.triggered.connect(callback); menu.addAction(item)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(lambda reason:self.restore_window() if reason in (QSystemTrayIcon.ActivationReason.Trigger,QSystemTrayIcon.ActivationReason.DoubleClick) else None)
            self.tray.messageClicked.connect(self.open_companion if desktop else self.restore_window)
            self.tray.show()
        QApplication.instance().installEventFilter(self)
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(500 if desktop else 50)
        self.motion_timer = QTimer(self); self.motion_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.motion_timer.timeout.connect(self.animate_scene)
        if not desktop: self.motion_timer.start(16)
        self.text()

    def setup_assistant(self, root):
        self.settings_button.hide()
        self.phone_dismiss = CloseButton(self, color='#F6EAEE')
        self.phone_dismiss.base_geometry = (1146,87,26,26)
        self.phone_dismiss.setAccessibleName('关闭陪伴手机')
        self.phone_dismiss.setToolTip('关闭陪伴手机')
        self.phone_dismiss.setStyleSheet('background:#28262E;border:0;border-radius:13px;padding:0;')
        self.phone_dismiss.clicked.connect(self.close)
        self.phone_grip = QWidget(self)
        self.phone_grip.setStyleSheet('background:transparent;')
        self.phone_drag = WindowDrag(self.phone_grip, self)
        self.reply_alert = QLabel(self)
        self.reply_alert.setObjectName('replyAlert')
        self.reply_alert.setAccessibleName('回复失败')
        self.reply_alert.setTextFormat(Qt.TextFormat.PlainText)
        self.reply_alert.setWordWrap(True)
        self.reply_alert.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.reply_alert.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.reply_alert.setStyleSheet(themed('QLabel#replyAlert{background:$alertBackground;color:$error;'
                                              'border:1px solid $alertBorder;border-radius:13px;padding:8px 10px;font-size:13px;}'))
        self.reply_alert.hide()
        self.reply_alert_timer = QTimer(self, singleShot=True, interval=8000)
        self.reply_alert_timer.timeout.connect(self.reply_alert.hide)
        self.settings_pane.setStyleSheet(phone_css())
        self.settings_window = FloatingPanel('设置', self.settings_pane, (420,654), self.close_assistant_panel)
        actions = (('companion','陪伴系统',self.open_companion),
                   ('screenshot','截图',self.start_screenshot),
                   ('workspace','若娜瓦加班区',self.toggle_workspace),
                   ('desktop-organizer','桌面整理',self.open_desktop_organizer),
                   ('settings','设置',self.settings))
        self.launcher = DesktopLauncher(self.assets, root/'design/desktop-assistant-icons', tuple(
            (key, title, show if key in ('screenshot','desktop-organizer','workspace') else lambda panel=key, show=show: self.panels.toggle(panel, show))
            for key, title, show in actions))
        self.launcher.set_pinned(self.controller.preferences.desktop_pinned)
        self.panels = PanelTransitions(self.launcher)

    def apply_theme(self, theme):
        if theme == self._theme:
            return
        from client_workspace_history import MessageText, TurnView
        from client_workspace import WorkspacePane
        old_colors = COLORS.copy()
        set_theme(theme)
        self._theme = theme
        widgets = QApplication.allWidgets()
        for widget in widgets:
            style = widget.styleSheet()
            if style and not getattr(widget, '_ronova_fixed_theme', False):
                prior = getattr(widget, '_ronova_theme_style', None)
                if prior is None or style != prior[2]:
                    original, source = style, old_colors
                else:
                    original, source = prior[:2]
                updated = recolor_style(original, source, COLORS)
                widget.setStyleSheet(updated)
                widget._ronova_theme_style = (original, source, updated)
            if isinstance(widget, MessageText):
                widget.refresh_theme()
            widget.update()
        for widget in widgets:
            if isinstance(widget, WorkspacePane):
                widget.refresh_workspace_theme()
            elif isinstance(widget, TurnView):
                widget.refresh_bubble_theme()
        surface = self.overlay.surface
        surface.setStyleSheet(phone_css() + (COMPACT_CHAT_CSS if surface.compact_chat else ''))
        if self.desktop:
            self.settings_pane.setStyleSheet(phone_css())
        self._scene_cache_signature = None
        self._scene_variants.clear()
        self.update()

    def open_companion(self):
        if self.controller.closing or self.capture_session is not None: return
        if not self.isVisible():
            state = self.controller.snapshot()
            if state['incoming'] or state['call_requested']:
                self.open_call()
            else:
                self.text()
            if not self.phone_positioned:
                anchor = self.launcher.mapToGlobal(self.launcher.center)
                area = available_at(anchor)
                height = min(PHONE_BODY[3], area.height()-24)
                self.resize(round(PHONE_WINDOW_WIDTH*height/PHONE_BODY[3]),height)
                self.move(constrain(anchor-QPoint(self.width()+102,self.height()-65),self.size(),area))
                self.phone_positioned = True
        self.panels.open('companion', self, self.hide_companion, self.show_companion)

    def show_companion(self):
        show_window(self)
        if self.overlay.page == 'text':
            self.overlay.focus_page()

    def hide_companion(self):
        state = self.controller.snapshot()
        keep_call = self.capture_opening_workspace and self.panels.requested and self.panels.requested[0]=='workspace'
        self.capture_opening_workspace = False
        if state['incoming'] and not keep_call:
            self.safe(lambda:self.controller.decline_call(state['incoming']['id']))
        elif not keep_call and (self.voice_start is not None or self.phone_submit is not None or
              any(state[key] for key in ('call_requested','active','responding','playing'))):
            self.safe(self.controller.end_call)
        self.text_pane.call_options.hide()
        self.reply_alert_timer.stop()
        self.reply_alert.hide()
        self.hide()
        if not keep_call: self.text()

    def close_assistant_panel(self, window):
        self.panels.close(window)

    def ensure_workspace(self):
        if self.workspace_window is None:
            from client_workspace import WorkspacePane
            from client_workspace_voice import WorkspaceVoice
            from client_workspace_window import WorkspaceWindow
            icon = self.asset_root/'design/desktop-assistant-icons/screenshot.svg'
            avatar = QPixmap(str(self.asset_root/'design/character-final/avatar-ronova-chibi-working.png'))
            if avatar.isNull(): avatar = self.assets.avatar
            if self.workspace_voice is None:
                self.workspace_voice = WorkspaceVoice(self.controller)
            self.workspace = WorkspacePane(avatar,icon,self.start_screenshot,self.codex,
                                           lambda:self.settings(page='models'),self.workspace_voice)
            self.workspace.task_completed.connect(self.launcher.show_task_completed)
            self.workspace_window = WorkspaceWindow(self.workspace,self.close_assistant_panel)
        return self.workspace

    def toggle_workspace(self):
        if self.workspace_window is not None and self.workspace_window.collapsed:
            self.open_workspace()
        else:
            self.panels.toggle('workspace',self.open_workspace)

    def open_workspace(self):
        if self.controller.closing or self.capture_session is not None: return
        self.ensure_workspace()
        self.codex.start()
        window = self.workspace_window
        if window.collapsed:
            window.set_collapsed(False)
        window.place_near(self.launcher.mapToGlobal(self.launcher.center))
        self.panels.open('workspace', window, self.hide_workspace, lambda: show_window(window))

    def hide_workspace(self):
        self.workspace_window.hide()

    def open_desktop_organizer(self):
        if self.controller.closing or self.capture_session is not None:
            return
        if self.desktop_organizer is None:
            from client_desktop_organizer import DesktopOrganizer
            self.desktop_organizer = DesktopOrganizer(self.controller.root)
        self.desktop_organizer.toggle(self.launcher.mapToGlobal(self.launcher.center))

    def organizer_windows(self):
        return self.desktop_organizer.windows() if self.desktop_organizer is not None else ()

    def start_screenshot(self):
        if self.capture_session is not None or self.controller.closing: return
        state = self.controller.snapshot()
        if state['incoming'] or state['oath_active']:
            self.notify('请先处理来电或完成誓约，再开始截图')
            return
        from client_capture import DesktopCapture
        self.capture_session = DesktopCapture(self.finish_screenshot)
        self.prepare_screenshot(self.capture_session)

    def prepare_screenshot(self, capture):
        if self.capture_session is not capture: return
        state = self.controller.snapshot()
        if self.panels.motion.frame is not None or state['busy']:
            QTimer.singleShot(25,lambda:self.prepare_screenshot(capture))
            return
        if state['incoming'] or state['oath_active']:
            capture.cancel()
            return
        if self.capture_audio is None and state['call_requested'] and state['active'] and not state['muted']:
            try: self.capture_audio = self.controller.pause_for_screenshot()
            except RuntimeError as exc:
                capture.finish((),str(exc)); return
            QTimer.singleShot(25,lambda:self.prepare_screenshot(capture))
            return
        if self.capture_audio is not None and not self.capture_audio.done():
            QTimer.singleShot(25,lambda:self.prepare_screenshot(capture))
            return
        if self.capture_audio is not None and not self.capture_audio.result() and state['active'] and not state['muted']:
            capture.finish((),'无法暂停通话收音，请重新截图')
            return
        self.capture_focus = QApplication.activeWindow()
        self.capture_windows = tuple(window for window in (self.launcher,self,self.settings_window,self.workspace_window,*self.organizer_windows())
                                     if window is not None and window.isVisible())
        for window in self.capture_windows: window.hide()
        if self.desktop_organizer is not None:
            self.desktop_organizer.set_capture_hidden(True)
        capture.open()

    def finish_screenshot(self, images, error):
        self.capture_session = None
        windows,self.capture_windows = self.capture_windows,()
        focus,self.capture_focus = self.capture_focus,None
        pause,self.capture_audio = self.capture_audio,None
        if self._quitting or self.controller.closing: return
        organizer = self.desktop_organizer
        for window in windows:
            if organizer is None or window not in organizer.windows() or organizer.desktop is not None:
                window.show()
        if organizer is not None: organizer.set_capture_hidden(False)
        if focus in windows and focus.isVisible() and not images and not error: show_window(focus)
        if pause is not None: self.safe(lambda:self.controller.resume_after_screenshot(pause))
        if images or error:
            workspace = self.ensure_workspace()
            if error: workspace.show_failure(error)
            else: workspace.attach_images(images)
            self.capture_opening_workspace = (self.panels.current is not None and self.panels.current[1] is self and
                                               self.controller.snapshot()['call_requested'])
            self.open_workspace()

    def eventFilter(self,receiver,event):
        if event.type() in (QEvent.Type.KeyPress,QEvent.Type.InputMethod) and isinstance(receiver,QWidget):
            if (receiver is self or self.isAncestorOf(receiver) or self.overlay.surface.isAncestorOf(receiver)
                    or any(window is not None and (receiver is window or window.isAncestorOf(receiver))
                           for window in (self.launcher,self.settings_window,self.workspace_window,*self.organizer_windows()))):
                if not self.controller.closing: self.controller.incoming_calls.activity()
        return super().eventFilter(receiver,event)

    def restore_window(self):
        if self.controller.closing or self.capture_session is not None: return
        if self.desktop:
            show_window(self.launcher)
            return
        self.showNormal(); self.raise_(); self.activateWindow()
        if self.controller.snapshot()['incoming']: self.open_call()

    def quit_client(self):
        self._quitting = True
        if self.tray: self.tray.hide()
        self.close()

    def button(self,name,geometry,action):
        button = MotionButton('',self,shape='circle' if geometry[2]==geometry[3] else 'rect')
        if name=='设置': button.feedback_shape='none'
        button.setAccessibleName(name); button.setToolTip(name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet('QPushButton{background:transparent;border:none;padding:0}QPushButton:focus{border:none}QPushButton:hover{border:none}')
        button.base_geometry = geometry
        button.feedback_changed.connect(lambda:self.update(button.geometry().adjusted(-3,-3,3,4)))
        button.clicked.connect(action)
        return button

    def resizeEvent(self,event):
        # A cached source frame belongs to one viewport and device scale.
        if hasattr(self,'_portrait_transition_from'): self._portrait_transition_from=None
        origin_x,origin_y,width,height = (PHONE_BODY[0],PHONE_BODY[1],PHONE_WINDOW_WIDTH,PHONE_BODY[3]) if self.desktop else (0,0,1280,802)
        self.scale = min(self.width()/width,self.height()/height)
        self.offset_x=(self.width()-width*self.scale)/2-origin_x*self.scale
        self.offset_y=(self.height()-height*self.scale)/2-origin_y*self.scale
        self.place_controls()
        if hasattr(self,'overlay'): self.overlay.place()

    def place_controls(self):
        for button in self.findChildren(QPushButton,options=Qt.FindChildOption.FindDirectChildrenOnly):
            x,y,w,h=button.base_geometry
            button.setGeometry(round(self.offset_x+x*self.scale),round(self.offset_y+y*self.scale),round(w*self.scale),round(h*self.scale))
        if hasattr(self,'affinity_bar'):
            x,y,w,h=self.affinity_bar.base_geometry
            self.affinity_bar.setGeometry(round(self.offset_x+x*self.scale),round(self.offset_y+y*self.scale),round(w*self.scale),round(h*self.scale))
        if self.desktop and hasattr(self,'phone_grip'):
            x,y,w,h=(887,87,166,80)
            self.phone_grip.setGeometry(round(self.offset_x+x*self.scale),round(self.offset_y+y*self.scale),
                                        round(w*self.scale),round(h*self.scale))
            self.place_reply_alert()
            self.raise_phone_chrome()

    def raise_phone_chrome(self):
        if self.desktop:
            self.phone_grip.raise_()
            self.phone_dismiss.raise_()
            self.reply_alert.raise_()

    def show_reply_alert(self, message, title='回复失败'):
        if self.desktop and self.isVisible():
            self.reply_alert.setAccessibleName(title)
            self.reply_alert.setText(title + '\n' + message[:220])
            self.place_reply_alert()
            self.reply_alert.show()
            self.reply_alert.raise_()
            self.reply_alert_timer.start()

    def place_reply_alert(self):
        width = round(296*self.scale)
        height = max(48,self.reply_alert.heightForWidth(width))
        self.reply_alert.setGeometry(round(self.offset_x+822*self.scale),round(self.offset_y+133*self.scale),
                                     width,height)

    def showEvent(self,event):
        super().showEvent(event)
        if hasattr(self,'timer'): self.timer.setInterval(50); self.motion_timer.start(16)
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def hideEvent(self,event):
        super().hideEvent(event)
        if hasattr(self,'timer'): self.timer.setInterval(1000); self.motion_timer.stop()

    def safe(self,action):
        try: action(); return True
        except Exception as exc:
            self.show_error(exc)
            return False

    def show_error(self,exc):
        message = str(exc) if isinstance(exc,(ValueError,RuntimeError,str)) else '操作未完成（'+type(exc).__name__+'）'
        self.controller.status(message)
        self.notify(message)

    def notify(self, message):
        if self.desktop and self.settings_window.isActiveWindow():
            self.settings_pane.status_label.setText(message)
        elif self.desktop and self.isVisible() and self.screen == 'call':
            QToolTip.showText(self.status_button.mapToGlobal(QPoint(0,0)),message,
                              self.status_button,self.status_button.rect(),5000)
        elif not self.desktop or self.isVisible():
            self.overlay.notify(message)

    def submit_phone_text(self, text):
        self.phone_submit = self.controller.submit(text)

    def voice(self):
        state = self.controller.snapshot()
        if not state['busy']:
            if state['incoming']: self.safe(lambda:self.controller.answer_call(state['incoming']['id']))
            else: self.safe(self.controller.mute if state['call_requested'] else self.controller.start_call)

    def end_or_decline(self):
        row = self.controller.snapshot()['incoming']
        self.safe(lambda:self.controller.decline_call(row['id'])) if row else self.safe(self.controller.end_call)
        if self.desktop: self.text()

    def text_or_later(self):
        state = self.controller.snapshot()
        row = state['incoming']
        if row: self.safe(lambda:self.controller.decline_call(row['id'],later=True))
        elif self.desktop and state['call_requested']: self.safe(self.controller.toggle_call_speaker)
        else: self.text()

    def status_action(self):
        if not self.state.get('voice_ready', True) and not self.state.get('voice_preparing'):
            self.safe(self.controller.retry_voice_prepare)
            return
        if self.state['incoming']: return
        if self.state['responding']: self.safe(self.controller.cancel)
        elif self.state['microphone_error']: self.safe(self.controller.start_call)
        else: self.text()

    def text(self):
        self.overlay.open_page('text')

    def set_screen(self, name):
        if self.screen == name: return
        if name!='call': self.stop_video()
        self.screen = name
        self._portrait_transition_from = None
        self.place_controls()
        self.update()

    def open_call(self):
        # Entering the phone is navigation; the microphone has its own button.
        self.overlay.close_page()

    def start_phone_voice(self, *, video=False):
        state = self.controller.snapshot()
        if state['busy'] or state['oath_active'] or not state.get('voice_ready',True): return
        if video:
            if state['incoming'] or self.video_call: return
            self.video_call = True
        self.open_call()
        if not state['incoming'] and not state['call_requested']:
            try:
                self.voice_start = self.controller.start_call(answer_incoming=False)
            except Exception as exc:
                if video:
                    self.text()
                self.show_error(exc)

    def stop_video(self):
        self.video_call = False

    def settings(self, *, page=None):
        if self.desktop:
            if self.capture_session is not None: return
            window = self.settings_window
            if not window.isVisible(): self.settings_pane.prepare()
            if page=='models': self.settings_pane.choose_models()
            window.place_near(self.launcher.mapToGlobal(self.launcher.center))
            self.panels.open('settings', window, self.hide_settings, lambda: show_window(window))
            return
        if self.overlay.target != 'settings': self.settings_origin = self.screen
        self.overlay.open_page('settings')

    def hide_settings(self):
        self.settings_pane.save_font_size()
        self.settings_window.hide()

    def sync_oath_entry(self, profile=None):
        profile=self.controller.memory.profile() if profile is None else profile
        completed=profile['oath_completed']
        eligible=profile['affinity']==100
        available=self.controller.oath_available()
        label='重温誓约' if completed else '誓约'
        if completed:
            hint='重温誓约 · 缔结于 '+datetime.fromtimestamp(profile['oath_completed_at']).strftime('%Y-%m-%d %H:%M')
            if not eligible: hint+='\n好感度达到100后可重温'
        else:
            hint='开始掌心相接的誓约' if eligible else '好感度达到100后可开启'
        if not available: hint+='\n请结束当前通话、回复、播放或保存操作后再开始'
        if not self.desktop:
            self.oath_button.setText(label)
            self.oath_button.setVisible(eligible)
        self.oath_button.setAccessibleName(label)
        self.oath_button.setToolTip(hint); self.oath_button.setAccessibleDescription(hint)
        self.oath_button.setEnabled(eligible and available)

    def open_oath(self):
        if self.oath_dialog and self.oath_dialog.isVisible():
            self.oath_dialog.raise_(); self.oath_dialog.activateWindow(); return
        self.sync_oath_entry()
        if not self.oath_button.isEnabled(): return
        if self.desktop: self.text_pane.call_options.hide()
        from client_oath_panel import OathDialog
        if self.oath_dialog: self.oath_dialog.deleteLater()
        self.oath_dialog=OathDialog(self)
        self.oath_dialog.finished.connect(self.oath_closed)
        self.oath_dialog.open()

    def oath_closed(self, _result):
        if not self.controller.closed and not self.controller.closing:
            self.sync_oath_entry()
            self.settings_pane.memory_page.refresh(force=True)

    def close_phone(self):
        if self.desktop:
            self.settings_window.close()
            return
        if self.overlay.page=='settings' and self.settings_origin=='home': self.text()
        else: self.open_call()

    def phone_controls(self,visible):
        for button in (self.voice_button,self.end_button,self.text_button,self.status_button):
            button.setVisible(visible and self.screen=='call')
        if self.video_call: self.status_button.hide()

    def refresh(self):
        editor = self.text_pane.input
        editing = editor.isVisible() and (editor.composing or not editor.document().isEmpty())
        events = self.controller.poll(interaction_busy=editing or self.capture_session is not None)
        if self.controller.closed:
            self.stop_video()
            self.timer.stop(); self.motion_timer.stop(); self.close()
            if self._quitting: QApplication.instance().quit()
            return
        if self.controller.closing:
            self.update(); return
        prior = self.state
        start_completed = self.voice_start is not None and self.voice_start.done()
        self.state=self.controller.snapshot()
        video_start_failed = start_completed and self.video_call and not self.state['call_requested']
        if start_completed:
            self.voice_start = None
        if self.video_call and not self.state['call_requested'] and (video_start_failed or prior['call_requested']):
            self.text()
        if self.phone_submit is not None and self.phone_submit.done():
            self.phone_submit = None
        if self.desktop:
            self.launcher.set_pinned(self.controller.preferences.desktop_pinned)
            interval = 50 if self.isVisible() or self.settings_window.isVisible() else 500
            if self.timer.interval() != interval: self.timer.setInterval(interval)
        self.sync_portrait(self.controller.memory.profile()['stage'])
        if self.state['call_requested'] and self.call_started is None: self.call_started=time.monotonic()
        if not self.state['call_requested']: self.call_started=None
        ringing = bool(self.state['incoming'])
        if self.tray:
            self.tray.setToolTip('若娜瓦 · '+('来电中' if ringing else '通话中' if self.state['call_requested'] else '等待来电' if self.controller.preferences.proactive_calls else '主动来电已关闭'))
        voice_label='接听' if ringing else '恢复麦克风' if self.state['muted'] else '静音' if self.state['call_requested'] else '语音通话'
        voice_ready=self.state.get('voice_ready',True)
        can_mute=self.state['call_requested'] and not self.state['muted'] and not ringing
        if not voice_ready and not can_mute:
            voice_label='语音加载中' if self.state.get('voice_preparing') else '语音未就绪'
        self.voice_button.setToolTip(voice_label); self.voice_button.setAccessibleName(voice_label)
        self.end_button.setToolTip('拒接' if ringing else '结束通话'); self.end_button.setAccessibleName('拒接' if ringing else '结束通话')
        text_label = '稍后' if ringing else '文字聊天'
        if self.desktop and self.state['call_requested'] and not ringing:
            text_label = '恢复扬声器' if self.state['speaker_muted'] else '静音扬声器'
        self.text_button.setToolTip('30分钟后提醒一次' if ringing else text_label)
        self.text_button.setAccessibleName(text_label)
        video_connecting = self.video_call and not self.state['call_requested']
        self.end_button.setEnabled(self.video_call or ringing or self.state['call_requested'] or self.state['responding'] or self.state['playing'])
        self.voice_button.setEnabled(not (self.state['busy'] or video_connecting) and (voice_ready or can_mute))
        self.text_button.setEnabled(not (self.state['busy'] or video_connecting))
        retry_voice=not voice_ready and not self.state.get('voice_preparing')
        self.status_button.setAccessibleName('重新加载语音' if retry_voice else '停止回应')
        self.status_button.setToolTip((self.state.get('voice_prepare_error') or '语音模型尚未就绪')+'；点击重新加载语音' if retry_voice else '点击停止回应')
        self.status_button.setEnabled(not self.state['busy'] and not (not voice_ready and self.state.get('voice_preparing')))
        self.overlay.refresh()
        profile=self.controller.memory.profile()
        self.affinity_bar.setValue(profile['affinity'])
        self.sync_oath_entry(profile)
        if profile['affinity']==100 and not profile['oath_completed'] and not self._oath_unlock_notified:
            self._oath_unlock_pending=True
        if self._oath_unlock_pending and (profile['affinity']<100 or profile['oath_completed']):
            self._oath_unlock_pending=False
        if self._oath_unlock_pending and self.isActiveWindow() and not self.state['responding'] and not self.state['oath_active']:
            if self.desktop:
                plus = self.text_pane.call_options.plus
                anchor = plus if plus.isVisible() else self.phone_grip
                hint = '好感度已满，可从聊天输入框旁的“＋”菜单开启誓约'
            else:
                anchor = self.oath_button
                hint = '好感度已满，点击手机左侧的爱心“誓约”开启仪式'
            QToolTip.showText(anchor.mapToGlobal(QPoint(0,-30)),hint,anchor,anchor.rect(),5000)
            self._oath_unlock_notified=True; self._oath_unlock_pending=False
        for event in events:
            if event['kind']=='incoming_call' and self.state['incoming'] and event['id']==self.state['incoming']['id']:
                if self.capture_session is not None: self.capture_session.cancel()
                self.open_call()
                if self.desktop: self.open_companion()
                else: self.restore_window()
                if self.tray: self.tray.showMessage('若娜瓦来电','点击打开客户端接听',QSystemTrayIcon.MessageIcon.Information,10000)
            elif event['kind']=='incoming_closed':
                self.controller.status({'missed':'未接来电','declined':'来电已拒接','snoozed':'稍后提醒已设置'}.get(event['state'],'来电已结束'))
            elif event['kind']=='incoming_ringtone_failed' and self.state['incoming'] and event['id']==self.state['incoming']['id']:
                self.notify(event['message'])
            elif event['kind']=='incoming_decision_failed':
                self.controller.status(event['message'])
                self.settings_pane.status_label.setText(event['message'])
                if self.isVisible(): self.show_reply_alert(event['message'],'主动回应失败')
            elif event['kind']=='operation_failed':
                if self.desktop and self.isVisible():
                    self.show_reply_alert(event['message'], '视频通话失败' if video_start_failed else '回复失败')
                else:
                    self.notify(event['message'])
            elif event['kind']=='failed' and event.get('error'):
                self.show_reply_alert(event['error'])
            elif event['kind']=='voice_failed':
                self.show_reply_alert(event['message'], '语音失败')
            elif event['kind']=='history_cleared':
                self.notify('聊天记录已清除')
        clock=phone_clock()
        second=int(time.monotonic()-self.call_started) if self.call_started else None
        changed=any(prior.get(key)!=value for key,value in self.state.items() if key!='playback_level')
        if changed or clock!=self._display_clock or second!=self._display_second:
            self.update_scene_rect((806,85,328,697))
        self._display_clock=clock; self._display_second=second

    def update_scene_rect(self, rect):
        x,y,w,h=rect
        dirty=QRectF(self.offset_x+x*self.scale,self.offset_y+y*self.scale,w*self.scale,h*self.scale)
        self.update(dirty.toAlignedRect().adjusted(-2,-2,2,2))

    def animate_scene(self):
        if self._portrait_transition_from is not None:
            if time.monotonic()-self._portrait_transition_started>=self._portrait_transition_duration:
                self._portrait_transition_from=None
            self.update()
        previous=self.speaking_motion.amount
        self.speaking_motion.tick(self.state['playing'],self.state['playback_level'])
        if abs(previous-self.speaking_motion.amount)>.0001:
            self.update_scene_rect((844,168,252,252))
            if self.overlay.isVisible() and self.overlay.page=='text':
                self.text_pane.avatar.update()
                if self.text_pane.history.active_avatar: self.text_pane.history.active_avatar.update()

    def scene_cache(self):
        dpr=self.devicePixelRatioF()
        signature=(self.width(),self.height(),dpr,self.scale,self.offset_x,self.offset_y,self.assets.stage)
        if self._scene_cache_signature!=signature:
            self._scene_variants.clear(); self._scene_cache_signature=signature
        phone=self.screen=='call'
        # Keep the two navigation backgrounds for this viewport and portrait.
        # Switching pages reuses them; resize/DPI/relationship changes rebuild.
        if phone not in self._scene_variants:
            cache=QPixmap(math.ceil(self.width()*dpr),math.ceil(self.height()*dpr))
            cache.setDevicePixelRatio(dpr)
            cache.fill(Qt.GlobalColor.transparent if self.desktop else QColor(self.colors['callBackdrop' if phone else 'background']))
            painter=QPainter(cache)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.translate(self.offset_x,self.offset_y); painter.scale(self.scale,self.scale)
            self.assets.paint_static(painter,phone=phone,companion=self.desktop); painter.end()
            self._scene_variants[phone]=cache
        self._scene_cache=self._scene_variants[phone]
        return self._scene_cache

    def portrait_progress(self):
        amount=min(1,max(0,(time.monotonic()-self._portrait_transition_started)/self._portrait_transition_duration))
        return amount*amount*(3-2*amount)

    def paint_scene_base(self, painter):
        target=self.scene_cache()
        if self._portrait_transition_from is None:
            painter.drawPixmap(0,0,target)
        else:
            painter.drawPixmap(0,0,self._portrait_transition_from)
            painter.save(); painter.setOpacity(self.portrait_progress())
            painter.drawPixmap(0,0,target); painter.restore()

    def sync_portrait(self, stage):
        stage='close' if stage=='bond' and 'bond' not in self.assets.portraits else stage
        stage=stage if stage in self.assets.portraits else 'acquaintance'
        if stage==self.assets.stage: return False
        source=self.scene_cache()
        if self._portrait_transition_from is not None:
            # A second change starts from what is actually on screen, rather
            # than flashing either the old or the unfinished target state.
            source=QPixmap(source.size()); source.setDevicePixelRatio(self.devicePixelRatioF())
            source.fill(Qt.GlobalColor.transparent)
            painter=QPainter(source); self.paint_scene_base(painter); painter.end()
        self.assets.set_stage(stage)
        self._portrait_transition_from=source
        self._portrait_transition_started=time.monotonic()
        self.update()
        return True

    def label(self,painter,text,rect,size=UI_FONT_SIZE,color='text',center=False,*,emphasis=False):
        font=emphasis_font() if emphasis else QFont()
        font.setPixelSize(scaled_size(size))
        painter.setFont(font); painter.setPen(QColor(self.colors.get(color,color)))
        alignment=Qt.AlignmentFlag.AlignTop|(Qt.AlignmentFlag.AlignHCenter if center else Qt.AlignmentFlag.AlignLeft)|Qt.TextFlag.TextWordWrap
        painter.drawText(QRectF(*rect),int(alignment),text)

    def paintEvent(self,event):
        painter=QPainter(self); self.paint_scene_base(painter)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(self.offset_x,self.offset_y); painter.scale(self.scale,self.scale)
        video = self.video_call and self.screen=='call'
        if self.screen=='call':
            if video: self.assets.paint_video(painter)
            self.assets.paint_dynamic(painter,self.speaking_motion,{'voice':self.voice_button.feedback,
                'end':self.end_button.feedback,'text':self.text_button.feedback},muted=self.state['muted'],
                incoming=bool(self.state['incoming']),
                speaker=self.state['speaker_muted'] if video or self.desktop and self.state['call_requested'] else None,
                video=video)
            if not self.state.get('voice_ready',True) and not (self.state['call_requested'] and not self.state['muted']):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(20,20,24,165))
                painter.drawEllipse(QRectF(836,660,60,60))
        if not self.desktop:
            self.label(painter,'RONOVA',(32,22,100,24),color='artworkText')
            self.label(painter,'若娜瓦',(140,24,90,24),13,'#ABB1B2')
            self.label(painter,'设置',(1202,22,48,24),14,'artworkText')
            self.label(painter,'死之执政',(39,618,180,24),14,'gold')
            self.label(painter,'若娜瓦',(36,648,320,62),28,'artworkText',emphasis=True)
            self.label(painter,'RONOVA',(39,713,160,22),12,'gold')
        if self.screen!='call': painter.end(); return
        self.assets.paint_phone_header(painter)
        self.label(painter,'若娜瓦',(870,133 if video else 399,200,46),24 if video else 28,'artworkText',center=True,emphasis=True)
        state=self.state
        calibrating=bool(state['active'] and '测量背景音' in state['message'])
        if self.controller.closing: status='正在退出'
        elif video and not state['call_requested']: status='正在接通'
        elif state.get('voice_prepare_error'): status='语音加载失败'
        elif state.get('voice_preparing'): status='正在准备语音'
        elif not state.get('voice_ready',True): status='语音未就绪'
        elif state['incoming']: status='若娜瓦来电'
        elif state['microphone_error']: status='麦克风不可用'
        elif state['muted']: status='麦克风已静音'
        elif state['playing']: status='正在说话'
        elif state['responding']: status='正在回应'
        elif calibrating: status='测量背景声音'
        elif state['microphone_state']=='preparing': status='正在准备收音'
        elif state['active']: status='正在聆听'
        elif state['message'] in ('未接来电','来电已拒接','稍后提醒已设置'): status=state['message']
        else: status='等待通话'
        self.label(painter,status,(879,167 if video else 483,182,28),color='artworkText',center=True)
        elapsed=int(time.monotonic()-self.call_started) if self.call_started else None
        timer='等待接听' if state['incoming'] else f'{elapsed//60:02d}:{elapsed%60:02d}' if elapsed is not None else '--:--'
        if not state.get('voice_ready',True):
            timer='加载完成后可通话' if state.get('voice_preparing') else '点击重新加载语音'
        self.label(painter,timer,(879,191 if video else 511,182,28),16,'artworkText',center=True)
        # Conversation text belongs to the text page. The call home stays quiet.
        if calibrating:
            self.label(painter,'先保持安静，约一秒后开始说话。',(846,572,248,24),12,'#DEE1DF',center=True)
        elif state.get('voice_prepare_error'):
            self.label(painter,state['voice_prepare_error'],(846,563,248,65),12,'#DEE1DF',center=True)
        elif state['incoming']:
            self.label(painter,state['incoming']['reason'],(846,563,248,65),13,'#DEE1DF',center=True)
        painter.end()

    def closeEvent(self,event):
        if self.controller.closed:
            self.stop_video()
            event.accept()
            return
        if self.desktop and not self._quitting:
            event.ignore()
            self.panels.close(self)
            return
        if self.tray and not self._quitting:
            event.ignore(); self.hide(); return
        self._quitting = True
        event.ignore()
        if self.capture_session is not None: self.capture_session.cancel()
        if self.workspace is not None: self.workspace.cancel_request(all_chats=True)
        if self.workspace_voice is not None: self.workspace_voice.close()
        if self.codex is not None: self.codex.close()
        self.stop_video()
        self.overlay.cancel_subpages(); self.overlay.transition.cancel(); self.overlay.hide()
        if self.desktop:
            self.panels.stop()
            if self.desktop_organizer is not None:
                self.desktop_organizer.hide()
            for window in (self.launcher,self.settings_window,self.workspace_window):
                if window is not None: window.hide()
        self.controller.close()
        self.setEnabled(False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true',help='检查依赖和素材后退出，不打开麦克风或调用模型')
    parser.add_argument('--autostart',action='store_true',help='由 Windows 登录启动项打开客户端')
    args=parser.parse_args()
    os.chdir(ROOT)
    app=QApplication(sys.argv[:1]); app.setApplicationName('Ronova')
    icon = ROOT/'design/desktop-assistant-icons/Ronova.ico'
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    font_family=load_fonts()
    if args.check:
        assets=SceneAssets(ROOT)
        from client_lore import LoreStore
        lore=LoreStore(ROOT)
        print(json.dumps({'qt':'ready','ui_font':font_family,'assets':len(assets.manifest['assets']),
                         'portrait_states':list(assets.portraits),'knowledge_ready':lore.enabled,
                         'knowledge_chunks':len(lore.chunks),'knowledge_stage':lore.stage,
                         'microphone_started':False,'cloud_requests':0}))
        return 0
    (ROOT/'data').mkdir(exist_ok=True)
    lock=QLockFile(str(ROOT/'data/client.lock'))
    lock.setStaleLockTime(0)
    endpoint='RonovaAssistant-'+sha256(str(ROOT).casefold().encode()).hexdigest()[:12]
    if not lock.tryLock(0):
        if not args.autostart:
            socket=QLocalSocket()
            socket.connectToServer(endpoint)
            if socket.waitForConnected(500):
                socket.disconnectFromServer()
            else:
                QMessageBox.information(None,'若娜瓦','客户端已经在运行。')
        return 0
    controller=None
    window=None
    server=QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    try:
        from client_autostart import WindowsAutostart
        if (ROOT/"data/desktop-organizer-restore.json").is_file():
            from client_desktop_organizer import DesktopOrganizer
            DesktopOrganizer(ROOT).recover()
        controller=DesktopController(ROOT,autostart=WindowsAutostart(ROOT))
        app.setQuitOnLastWindowClosed(False)
        window=ClientWindow(controller, desktop=True); window.restore_window()
        def restore_existing():
            connection=server.nextPendingConnection()
            if connection is not None:
                connection.close()
                connection.deleteLater()
                window.restore_window()
        server.newConnection.connect(restore_existing)
        server.listen(endpoint)
        window.safe(controller.sync_autostart)
        return app.exec()
    except Exception as exc:
        message = ('客户端启动失败：'+str(exc)+'\n请检查界面素材配置。' if isinstance(exc,SceneAssetError) else
                   '客户端启动失败（'+type(exc).__name__+'）。请检查本地配置与依赖。')
        QMessageBox.critical(None,'启动未完成',message)
        return 1
    finally:
        if window is not None and window.desktop_organizer is not None:
            window.desktop_organizer.hide()
        server.close()
        if controller and not controller.closed: controller.close().result(timeout=20)
        lock.unlock()


if __name__=='__main__': sys.exit(main())
