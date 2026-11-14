"""Project chats and screenshot drafts, separate from companionship."""
from dataclasses import dataclass, field
from functools import cached_property
from hashlib import sha256
from itertools import count
import json
from pathlib import Path
import uuid

from PySide6.QtCore import (QByteArray, QBuffer, QIODevice, QRectF, QSignalBlocker, QSize,
                            QStandardPaths, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import QBrush, QDesktopServices, QIcon, QImage, QKeySequence, QPainter, QPixmap, QShortcut, QTransform
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QStyle,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from chat_client import ChatError, IMAGE_LIMIT
from client_fonts import code_font, emphasis_font
from client_markup import MarkupEditor, marked_image
from client_panels import ChatInput, SendButton
from client_shell import CloseButton
from client_theme import COLORS, themed
from client_tool_library import discover_library
from client_workspace_controls import (CodexQuota, ContextButton, LibrarySection, MenuButton, ModelPicker, PermissionPicker,
                                       PicturePreview, ProjectSidebar, WorkspaceTree, mount_panel, tool_icon, usage_percent)
from client_workspace_history import WorkspaceHistory
from client_workspace_voice import WorkspaceVoiceBusy


CODEX_ITEMS = ('agentMessage','commandExecution','fileChange','plan')
USER_INPUT_REQUEST = 'item/tool/requestUserInput'
PERMISSIONS = {
    'manual': ('请求批准','写入项目之外或访问网络需要你批准'),
    'review': ('帮我批准','由 Codex 审查，需要你决定时再询问'),
    'read': ('只读','不写入项目文件，也不请求额外权限'),
}
EFFORT_NAMES = {'none':'无','minimal':'极低','low':'低','medium':'中','high':'高','xhigh':'极高',
                'max':'最大','ultra':'超高'}


@dataclass(eq=False)
class Picture:
    source: QImage
    marks: tuple = ()
    key: str = field(default_factory=lambda:uuid.uuid4().hex)

    @cached_property
    def preview(self):
        return marked_image(self.source,self.marks) if self.marks else self.source

    @cached_property
    def png(self):
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not self.preview.save(buffer,'PNG'):
            raise ChatError('截图无法编码，请重新截取')
        buffer.close()
        if len(data)>IMAGE_LIMIT:
            raise ChatError('截图超过 10 MiB，请缩小选区后再发送')
        return bytes(data)


@dataclass(eq=False)
class WorkspaceTurn:
    question: str
    pictures: tuple[Picture, ...] = ()
    state: str = 'pending'
    items: dict = field(default_factory=dict)
    codex_id: str | None = None


@dataclass(eq=False)
class WorkspaceChat:
    item: QTreeWidgetItem
    turns: list[WorkspaceTurn] = field(default_factory=list)
    draft: str = ''
    pictures: tuple[Picture, ...] = ()
    request: 'CodexRun | None' = None
    failed_turn: WorkspaceTurn | None = None
    alert: str = ''
    thread_id: str | None = None
    history_loaded: bool = True
    loading_history: bool = False
    deleting: bool = False
    server_deleted: bool = False
    delete_files: set[Path] = field(default_factory=set)
    renaming: bool = False
    pending_name: str = ''
    compacting: bool = False
    usage: tuple[int,int | None] | None = None
    permission: str = 'manual'
    model: str | None = None
    effort: str | None = None
    plan_mode: bool = False
    codex_used_plan_mode: bool = False


@dataclass(eq=False)
class CodexRun:
    chat: WorkspaceChat
    turn: WorkspaceTurn
    cwd: Path
    options: dict
    plan_mode: bool = False
    turn_sent: bool = False
    turn_id: str | None = None
    stopping: bool = False
    error: str = ''
    approvals: dict = field(default_factory=dict)
    question_dialog: tuple | None = None


class WorkspaceInput(ChatInput):
    pasted_image = Signal(QImage)

    def set_voice_preparation(self, text):
        self.empty_placeholder = text or '和若娜瓦一起工作…'
        self.update_placeholder()

    def canInsertFromMimeData(self, source):
        return source.hasImage() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        if source.hasImage():
            image = source.imageData()
            self.pasted_image.emit(image.toImage() if isinstance(image,QPixmap) else QImage(image))
        else:
            super().insertFromMimeData(source)


class ProjectDialog(QDialog):
    def __init__(self, parent, title, action):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(title)
        self.setAccessibleName(title)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(360)
        self.setStyleSheet(themed('''
            QFrame#projectDialogCard { background:$selection; border:1px solid $border; border-radius:22px; }
            QLabel { color:$text; background:transparent; }
            QLineEdit { color:$text; background:$surface; border:1px solid $gray;
                border-radius:10px; padding:7px 9px; selection-background-color:$selection; selection-color:$text; }
            QPushButton { color:$text; background:$surface; border:1px solid $border; border-radius:10px; }
            QPushButton:hover, QPushButton:focus { border-color:$gray; }
            QPushButton#projectDialogAction { color:$surface; background:$primary; border-color:$primary; }
            QPushButton#projectDialogAction:hover, QPushButton#projectDialogAction:focus {
                background:$primaryHover; border-color:$primaryHover; }
        '''))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName('projectDialogCard')
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 17, 20, 17)
        layout.setSpacing(10)
        heading = QLabel(title)
        heading.setFont(emphasis_font())
        layout.addWidget(heading)
        self.body = QVBoxLayout()
        self.body.setSpacing(6)
        layout.addLayout(self.body)
        buttons = QDialogButtonBox()
        self.confirm = buttons.addButton(action, QDialogButtonBox.ButtonRole.AcceptRole)
        self.confirm.setObjectName('projectDialogAction')
        self.cancel = buttons.addButton('取消', QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class WorkspaceBackdrop(QWidget):
    def __init__(self, avatar):
        super().__init__()
        self.avatar = avatar

    def paintEvent(self, event):
        if self.avatar.isNull():
            return
        size = min(self.width(), self.height())
        target = QRectF((self.width()-size)/2, (self.height()-size)/2, size, size)
        source = self.avatar.deviceIndependentSize()
        artwork = QBrush(self.avatar)
        artwork.setTransform(QTransform().translate(target.x(), target.y()).scale(
            size/source.width(), size/source.height()))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(.16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(artwork)
        painter.drawEllipse(target)


class WorkspacePane(QWidget):
    INNER_CORNER_RADIUS = 20

    def __init__(self, avatar, screenshot_icon, capture, codex, open_codex_settings, voice=None):
        super().__init__()
        self.codex = codex
        self.voice = voice
        self.playing_turn = None
        self.voice_note = ''
        self.codex_chats = {}
        self.deleted_threads = set()
        self.projects = {}
        self.project_preferences = self.codex.home/'ronova-projects.json'
        self.project_names, self.unassigned_hidden = self.load_project_preferences()
        self.deleting_project = None
        self.chats = {}
        self.active_chat = None
        self.editing_picture = None
        self.catalog = {}
        self.catalog_state = ''
        icons = Path(screenshot_icon).parent
        self.folder_icon = QIcon(str(icons/'folder.svg'))
        self.more_icon = tool_icon('<path d="M5 12h.01m6.99 0h.01m6.99 0h.01" stroke-width="2.7"/>',COLORS['muted'])
        self.rename_icon = tool_icon('<path d="m4 15 11-11 5 5L9 20H4v-5zm9-9 5 5"/>',COLORS['muted'])
        self.delete_icon = tool_icon('<path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',COLORS['primary'])
        self.approval_icons = {
            'manual': QIcon(str(icons/'approval-request.svg')),
            'review': QIcon(str(icons/'permissions.svg')),
            'read': QIcon(str(icons/'approval-read.svg')),
        }
        mic_glyph = ('<path d="M12 3.5a2.5 2.5 0 0 1 2.5 2.5v5a2.5 2.5 0 0 1-5 0V6A2.5 2.5 0 0 1 12 3.5z"/>'
                     '<path d="M6.5 11.5a5.5 5.5 0 0 0 11 0"/><path d="M12 17v3.5m-2.9 0h5.8"/>')
        self.mic_icon = tool_icon(mic_glyph,COLORS['muted'])
        self.mic_icon_active = tool_icon(mic_glyph,COLORS['text'])
        speaker_glyph = ('<path d="M4.5 9.5h3l4-3.6v12.2l-4-3.6h-3z"/>'
                         '<path d="M15.4 8.6a4.6 4.6 0 0 1 0 6.8"/>')
        reply_icons = (tool_icon(speaker_glyph,COLORS['gray']),
                       tool_icon('<rect x="7" y="7" width="10" height="10" rx="1.5"/>',COLORS['gray']))
        self.setStyleSheet(themed('''
            QWidget { color:$text; }
            QWidget#workChat { background:$surface; border-radius:WORK_RADIUSpx; }
            QPlainTextEdit, QTextBrowser { selection-background-color:$selection; selection-color:$text; }
            QMenu { background:$surface; color:$text; border:1px solid $border; }
            QMenu::item:selected { background:$selection; }
            QFrame#workSidebar { background:$background; border:1px solid $border; border-radius:WORK_RADIUSpx; }
            QFrame#workSidebar[folderDropActive="true"] { background:$selection; border-color:$gray; }
            QFrame#workLibrary { background:$surface; border:1px solid $border; border-radius:12px; }
            QPushButton#workLibraryHeader { background:transparent; text-align:left; padding:5px 9px; }
            QPushButton#workLibraryHeader:hover { background:$selection; }
            QListWidget#workLibraryItems { background:transparent; border:0; outline:0; font-size:13px; }
            QListWidget#workLibraryItems::item { height:25px; padding-left:5px; }
            QTreeWidget { background:transparent; border:0; outline:0; }
            QTreeWidget::item { height:30px; padding:0 5px; }
            QTreeWidget::item:selected { background:transparent; color:$text; }
            QLabel#workSection { color:$muted; font-size:12px; }
            QLabel#workChatTitle { font-size:16px; }
            QLabel#workHint { color:$muted; font-size:12px; }
            QFrame#workCodexCard { background:$background; border:1px solid $border; border-radius:12px; }
            QFrame#workAlert { background:$alertBackground; border:1px solid $alertBorder; border-radius:12px; }
            QFrame#workAlert QLabel { color:$error; }
            QScrollArea#workHistory, QWidget#workHistoryBody, QTextBrowser#workMessageText {
                background:transparent; border:0; }
            QFrame#workUserBubble { background:$elevated; border-radius:16px; }
            QFrame#workUserBubble QTextBrowser { color:$text; }
            QToolButton { background:transparent; padding:5px 7px; border:0; border-radius:8px; }
            QToolButton:hover { background:$selection; }
            QToolButton:disabled { color:$disabled; }
            QToolButton::menu-indicator { width:0; }
            QToolButton#workAddAction::menu-indicator { image:none; width:0; }
            QMenu#workControlMenu, QMenu#workModelMenu { background:$surface; border:1px solid $border; }
            QMenu#workControlMenu { padding:0; }
            QMenu#workModelMenu { padding:4px; border-radius:9px; }
            QWidget#workPermissionPicker, QWidget#workModelPicker { background:$surface; }
            QLabel#workPopupHint { color:$muted; font-size:12px; }
            QPushButton#workPermissionHelp { background:transparent; color:$muted; padding:2px 0;
                font-size:12px; text-decoration:underline; }
            QPushButton#workPermissionHelp:hover { color:$text; }
            QPushButton#workPermissionRow { background:transparent; border-radius:9px; padding:0; }
            QPushButton#workPermissionRow[chosen="true"] { background:$elevated; }
            QPushButton#workPermissionRow:hover, QPushButton#workPermissionRow:focus { background:$selection; }
            QLabel#workPermissionCheck, QLabel#workCurrentEffort { color:$muted; }
            QLabel#workCurrentEffort { font-size:14px; }
            QToolButton#workEffortReset { font-size:18px; padding:0; }
            QToolButton#workModelChoice { background:transparent; border:0; padding:2px 8px; }
            QToolButton#workModelChoice:hover, QToolButton#workModelChoice:focus {
                background:$elevated; border-radius:7px; }
            QListWidget#workModelList { background:transparent; border:0; outline:0; }
            QListWidget#workModelList::item { padding:0 9px; border-radius:8px; }
            QListWidget#workModelList::item:hover, QListWidget#workModelList::item:selected {
                background:$selection; color:$text; }
            QToolButton#workChatMenu { padding:2px 0 0; }
            QToolButton#workTrace { color:$muted; padding:5px 8px; }
            QPlainTextEdit#workTraceDetail { background:$background; border:1px solid $border;
                border-radius:10px; padding:8px; }
            QFrame#workComposer { background:$surface; border:1px solid $border; border-radius:WORK_RADIUSpx; }
            QPlainTextEdit#workInput { background:transparent; border:0; padding:2px 3px;
                placeholder-text-color:$muted; }
            QScrollArea#workAttachment { background:$background; border:0; border-radius:12px; }
            QWidget#workAttachmentBody { background:transparent; }
            QScrollArea#workAttachment QScrollBar:horizontal { height:7px; background:transparent; margin:0 7px; }
            QScrollArea#workAttachment QScrollBar::handle:horizontal { background:$disabled; border-radius:3px; min-width:24px; }
            QScrollArea#workAttachment QScrollBar::add-line, QScrollArea#workAttachment QScrollBar::sub-line { width:0; }
            QPushButton { background:$elevated; border:0; border-radius:9px; padding:6px 10px; }
            QPushButton:hover { background:$selection; }
            QPushButton#workNewChat { background:$surface; text-align:left; }
            QPushButton#workNewChat:hover { background:$selection; }
            QToolButton#workAddAction, QPushButton#workCapture { background:transparent; padding:0; border-radius:9px; }
            QToolButton#workAddAction:hover, QToolButton#workAddAction:focus, QToolButton#workAddAction:pressed,
            QPushButton#workCapture:hover, QPushButton#workCapture:focus { background:$elevated; }
            QToolButton#workAddAction[planMode="true"] { background:$alertBackground; }
            QPushButton#workMic, QPushButton#workSpeaker { background:transparent; padding:0; }
            QPushButton#workMic:hover, QPushButton#workMic:focus,
            QPushButton#workSpeaker:hover, QPushButton#workSpeaker:focus {
                background:$elevated; border-radius:9px; }
            QPushButton#workMic[listening="true"] { background:$elevated; border-radius:9px; }
            QPushButton#workAddProject { background:transparent; border:0;
                border-radius:8px; padding:0; }
            QPushButton#workAddProject:hover { background:$selection; }
            QPushButton#workDismissAlert { border-radius:14px; padding:0; }
            QPushButton#workRemoveImage { background:rgba(48,43,45,213);
                border:0; border-radius:9px; padding:0; }
            QPushButton#workRemoveImage:hover { background:$primary; }
            QPushButton#workStop { background:$primary; border-radius:17px; padding:0; }
            QWidget#workQuota { border-top:1px solid $border; }
            QLabel#workQuotaTitle { color:$muted; font-size:12px; }
            QLabel#workQuotaValue { color:$text; font-size:12px; }
            QProgressBar#workQuotaBar { background:$border; border:0; border-radius:3px; }
            QProgressBar#workQuotaBar::chunk { background:$primary; border-radius:3px; }
        ''').replace('WORK_RADIUS',str(self.INNER_CORNER_RADIUS)))
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0,0,0,0)
        outer.setSpacing(18)
        self.sidebar = self.build_sidebar()
        outer.addWidget(self.sidebar)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages,1)
        self.chat = QWidget(); self.chat.setObjectName('workChat'); self.pages.addWidget(self.chat)
        self.editor = MarkupEditor(); self.pages.addWidget(self.editor)
        self.editor.accepted.connect(self.save_marks); self.editor.cancelled.connect(self.close_editor)
        body = QVBoxLayout(self.chat); body.setContentsMargins(0,0,0,0); body.setSpacing(8)

        self.chat_title = QLabel()
        self.chat_title.setObjectName('workChatTitle')
        self.chat_title.setTextFormat(Qt.TextFormat.PlainText)
        self.chat_title.setFont(emphasis_font())
        self.chat_title.setWordWrap(True)
        body.addWidget(self.chat_title)

        self.account_banner = QFrame()
        self.account_banner.setObjectName('workCodexCard')
        account_row = QHBoxLayout(self.account_banner)
        account_row.setContentsMargins(9,6,9,6)
        self.account_text = QLabel()
        self.account_text.setWordWrap(True)
        self.account_text.setTextFormat(Qt.TextFormat.PlainText)
        account_row.addWidget(self.account_text,1)
        login = QPushButton('前往登录')
        login.setObjectName('workCodexLogin')
        login.clicked.connect(open_codex_settings)
        account_row.addWidget(login)
        body.addWidget(self.account_banner)

        self.alert = QFrame(); self.alert.setObjectName('workAlert')
        alert_row = QHBoxLayout(self.alert); alert_row.setContentsMargins(9,6,9,6)
        self.alert_text = QLabel(); self.alert_text.setWordWrap(True); self.alert_text.setTextFormat(Qt.TextFormat.PlainText)
        alert_row.addWidget(self.alert_text,1)
        self.retry = QPushButton('重试'); self.retry.clicked.connect(self.retry_request); alert_row.addWidget(self.retry)
        dismiss = CloseButton()
        dismiss.setObjectName('workDismissAlert')
        dismiss.setFixedSize(28,28)
        dismiss.setAccessibleName('关闭提示'); dismiss.clicked.connect(self.dismiss_alert)
        alert_row.addWidget(dismiss); body.addWidget(self.alert); self.alert.hide()

        backdrop = WorkspaceBackdrop(avatar)
        conversation = QVBoxLayout(backdrop)
        conversation.setContentsMargins(0,0,0,0)
        self.history = WorkspaceHistory(self.add_picture, reply_icons if voice is not None else None)
        self.history.play_requested.connect(self.play_reply)
        conversation.addWidget(self.history)
        body.addWidget(backdrop,1)

        self.approval_card = QFrame()
        self.approval_card.setObjectName('workCodexCard')
        approval_layout = QVBoxLayout(self.approval_card)
        approval_layout.setContentsMargins(10,7,10,7)
        self.approval_title = QLabel()
        self.approval_title.setObjectName('workChatTitle')
        self.approval_title.setFont(emphasis_font())
        self.approval_title.setWordWrap(True)
        approval_layout.addWidget(self.approval_title)
        self.approval_detail = QPlainTextEdit(readOnly=True)
        self.approval_detail.setFont(code_font())
        self.approval_detail.setProperty('_ronova_code_font', True)
        self.approval_detail.setAccessibleName('Codex 请求审批的操作与目录')
        self.approval_detail.setFixedHeight(72)
        approval_layout.addWidget(self.approval_detail)
        actions = QHBoxLayout()
        actions.addStretch()
        self.reject = QPushButton('拒绝')
        self.reject.clicked.connect(lambda: self.answer_approval(False))
        self.allow = QPushButton('允许本次')
        self.allow.clicked.connect(lambda: self.answer_approval(True))
        actions.addWidget(self.reject)
        actions.addWidget(self.allow)
        approval_layout.addLayout(actions)
        body.addWidget(self.approval_card)

        composer = QFrame()
        composer.setObjectName('workComposer')
        compose = QVBoxLayout(composer)
        compose.setContentsMargins(13,10,11,9)
        compose.setSpacing(5)
        body.addWidget(composer)

        self.attachment = QScrollArea(); self.attachment.setObjectName('workAttachment')
        self.attachment.setWidgetResizable(True)
        self.attachment.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        attachment_body = QWidget(); attachment_body.setObjectName('workAttachmentBody')
        self.attachment_row = QHBoxLayout(attachment_body)
        self.attachment_row.setContentsMargins(8,5,8,5); self.attachment_row.setSpacing(8)
        self.attachment_row.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.attachment.setWidget(attachment_body)
        compose.addWidget(self.attachment); self.attachment.hide()

        self.input = WorkspaceInput()
        self.input.setObjectName('workInput')
        self.input.set_voice_preparation('')
        self.input.setFixedHeight(61)
        self.input.setTabChangesFocus(True)
        self.input.setAccessibleName('工作区问题'); self.input.send_requested.connect(self.submit)
        self.input.pasted_image.connect(self.attach_image); self.input.textChanged.connect(self.refresh_controls)
        compose.addWidget(self.input)

        controls = QHBoxLayout(); compose.addLayout(controls)
        self.add_action = MenuButton('workAddAction', '更多操作', self.composer_menu)
        self.add_action.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        plus = '<path d="M12 5v14M5 12h14"/>'
        self.composer_icons = (tool_icon(plus), tool_icon(plus, COLORS['primary']))
        self.compact_icon = tool_icon('<path d="M5 12h14M12 3v6m-3-3 3 3 3-3M12 21v-6m-3 3 3-3 3 3"/>')
        self.plan_icon = tool_icon('<rect x="5" y="4" width="14" height="16" rx="2"/>'
                                   '<path d="m8 9 1 1 2-2m2 2h3m-8 5h8"/>')
        controls.addWidget(self.add_action)
        take = QPushButton(QIcon(str(screenshot_icon)), '')
        take.setObjectName('workCapture')
        for control in (self.add_action, take):
            control.setIconSize(QSize(22,22))
            control.setFixedSize(34,34)
        take.setToolTip('框选截图'); take.setAccessibleName('截图'); take.clicked.connect(capture); controls.addWidget(take)
        self.permission_button = MenuButton('workPermission','命令审批方式',self.permission_menu)
        self.permission_button.setIconSize(QSize(15,15))
        controls.addWidget(self.permission_button)
        self.status = QLabel(); self.status.setObjectName('workHint'); controls.addWidget(self.status,1)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.context_button = ContextButton(self.context_menu)
        self.model_button = MenuButton('workModel','Codex 模型与推理强度',self.model_menu)
        self.model_button.setFont(emphasis_font())
        controls.addWidget(self.context_button)
        controls.addWidget(self.model_button)
        self.mic = QPushButton(self.mic_icon,'')
        self.mic.setObjectName('workMic')
        self.mic.setIconSize(QSize(22,22)); self.mic.setFixedSize(34,34)
        self.mic.setCheckable(True)
        self.mic.setToolTip('语音提问：点击开始收音，再点停止并把转写放进输入框')
        self.mic.setAccessibleName('语音提问')
        self.mic.clicked.connect(self.toggle_dictation)
        controls.addWidget(self.mic)
        self.send = SendButton(self.submit, arrow=True)
        stop_icon = tool_icon(f'<rect x="6" y="6" width="12" height="12" rx="1.6" '
                              f'fill="{COLORS["surface"]}" stroke="none"/>')
        self.stop = QPushButton(stop_icon, '')
        self.stop.setObjectName('workStop')
        self.stop.setAccessibleName('停止回答')
        self.stop.setToolTip('停止回答')
        self.stop.setIconSize(QSize(20,20))
        self.stop.setFixedSize(34,34)
        self.stop.clicked.connect(lambda: self.cancel_request())
        controls.addWidget(self.send); controls.addWidget(self.stop)

        if self.voice is not None:
            self.voice.transcript_ready.connect(self.receive_transcript)
            self.voice.recognized_partial.connect(self.show_partial_transcript)
            self.voice.status_changed.connect(self.show_voice_status)
            self.voice.preparation_changed.connect(self.show_voice_preparation)
            self.voice.listening_changed.connect(self.dictation_changed)
            self.voice.playback_changed.connect(self.reply_playback_changed)

        self.redraw = QTimer(self,singleShot=True,interval=45); self.redraw.timeout.connect(self.render_history)
        if not self.unassigned_hidden:
            project = self.add_project(None,select=False)
            self.tree.setCurrentItem(self.add_chat(project).item)
            self.input.setFocus()
        else:
            self.chat_title.setText('选择或新建聊天')
            self.refresh_controls()
        for path in self.project_names:
            if path is not None:
                self.add_project(path,select=False)
        self.recover_media_cleanup()
        codex.ready.connect(self.load_codex_threads)
        codex.disconnected.connect(self.codex_disconnected)
        codex.account_changed.connect(self.codex_account_changed)
        codex.event.connect(self.codex_event)
        codex.approval.connect(self.codex_approval)
        if codex.connected:
            self.load_codex_threads()
            self.codex_account_changed()

    def build_sidebar(self):
        sidebar = ProjectSidebar()
        sidebar.folders_dropped.connect(self.add_dropped_projects)
        sidebar.setFixedWidth(196)
        column = QVBoxLayout(sidebar)
        column.setContentsMargins(9,10,9,8)
        column.setSpacing(8)
        self.library_timer = QTimer(self,interval=1500)
        self.library_timer.timeout.connect(self.refresh_library)
        self.library_sections = (
            LibrarySection('MCP','<path d="M8 6h8v4H8zM8 14h8v4H8zM12 10v4M5 8h3m8 8h3"/>',
                           self.sync_library_refresh),
            LibrarySection('Skills','<path d="M4 6c4-1 6 0 8 2 2-2 4-3 8-2v12c-4-1-6 0-8 2-2-2-4-3-8-2zm8 2v12"/>',
                           self.sync_library_refresh),
        )
        for section in self.library_sections:
            column.addWidget(section)
        plus = tool_icon('<path d="M12 5v14M5 12h14"/>',COLORS['muted'])
        new = QPushButton(plus,'新聊天')
        new.setIconSize(QSize(20,20))
        new.setObjectName('workNewChat')
        new.setFont(emphasis_font())
        new.clicked.connect(lambda: self.new_chat(self.current_project()))
        column.addWidget(new)

        heading = QHBoxLayout()
        section = QLabel('项目')
        section.setObjectName('workSection')
        section.setFont(emphasis_font())
        heading.addWidget(section,1)
        add = QPushButton(plus,'')
        add.setIconSize(QSize(19,19))
        add.setObjectName('workAddProject')
        add.setFixedSize(26,26)
        add.setAccessibleName('添加项目文件夹')
        add.setToolTip('选择文件夹，也可以直接拖到项目区')
        add.clicked.connect(self.choose_folder)
        heading.addWidget(add)
        column.addLayout(heading)

        self.tree = WorkspaceTree()
        self.tree.setAccessibleName('项目与聊天')
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1,QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1,24)
        self.tree.setMouseTracking(True)
        self.tree.setIndentation(12)
        self.tree.setUniformRowHeights(True)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.currentItemChanged.connect(self.select_chat)
        self.tree.itemDoubleClicked.connect(self.open_folder)
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete),self.tree)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self.delete_selected_chat)
        column.addWidget(self.tree,1)
        self.quota = CodexQuota(self.codex)
        column.addWidget(self.quota)
        return sidebar

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_library_refresh()

    def hideEvent(self, event):
        self.library_timer.stop()
        self.stop_voice()
        super().hideEvent(event)

    def closeEvent(self, event):
        self.stop_voice()
        super().closeEvent(event)

    def stop_voice(self):
        """Closing or hiding the workspace stops its microphone and playback."""
        if self.voice is None:
            return
        try:
            self.voice.stop_dictation()
            self.stop_playback()
        except Exception:
            pass
        self.voice_note = ''

    def sync_library_refresh(self):
        if self.isVisible() and any(section.header.isChecked() for section in self.library_sections):
            self.refresh_library()
            self.library_timer.start()
        else:
            self.library_timer.stop()

    def refresh_library(self):
        entries = discover_library(Path(__file__).resolve().parent)
        for section, items in zip(self.library_sections,entries):
            section.set_entries(items)

    @property
    def pictures(self):
        return self.active_chat.pictures if self.active_chat is not None else ()

    @property
    def turns(self):
        return self.active_chat.turns if self.active_chat is not None else []

    @property
    def request(self):
        return self.active_chat.request if self.active_chat is not None else None

    def current_project(self):
        item = self.tree.currentItem()
        return (item.parent() or item) if item is not None else self.projects.get(None)

    def choose_folder(self):
        project = self.current_project()
        start = (project.data(0,Qt.ItemDataRole.UserRole) or '') if project is not None else ''
        directory = QFileDialog.getExistingDirectory(self,'选择项目文件夹',start)
        if directory:
            self.add_project(Path(directory))

    def add_dropped_projects(self, folders):
        for folder in folders:
            self.add_project(folder)

    def load_project_preferences(self):
        try:
            preferences = json.loads(self.project_preferences.read_text(encoding='utf-8'))
            names = {Path(path) if path else None: name for path,name in preferences.get('names',{}).items()
                     if isinstance(name,str) and name.strip()}
            return names, preferences.get('unassigned_hidden') is True
        except (OSError, ValueError, AttributeError):
            return {}, False

    def save_project_preferences(self):
        preferences = {'names':{str(path) if path is not None else '':name for path,name in self.project_names.items()},
                       'unassigned_hidden':self.unassigned_hidden}
        try:
            self.project_preferences.parent.mkdir(parents=True,exist_ok=True)
            temporary = self.project_preferences.with_suffix('.tmp')
            temporary.write_text(json.dumps(preferences,ensure_ascii=False),encoding='utf-8')
            temporary.replace(self.project_preferences)
        except OSError:
            QMessageBox.warning(self,'无法保存项目设置','本次改动已显示，重启后可能需要重新设置。')

    def sidebar_menu(self, label, populate):
        button = MenuButton('workChatMenu',label,populate)
        button.setIcon(self.more_icon)
        button.setIconSize(QSize(17,17))
        button.setFixedSize(23,26)
        return button

    def add_project(self, path, *, select=True):
        if path in self.projects:
            project = self.projects[path]
            if select:
                self.tree.setCurrentItem(project)
            return project
        default_name = (path.name or str(path)) if path is not None else '未选项目'
        name = self.project_names.get(path,default_name)
        project = QTreeWidgetItem([name])
        project.setIcon(0,self.folder_icon)
        if path:
            project.setData(0,Qt.ItemDataRole.UserRole,str(path))
            project.setToolTip(0,f'{path}\n双击打开文件夹')
        self.tree.addTopLevelItem(project)
        self.projects[path] = project
        more = self.sidebar_menu('项目操作',lambda menu: self.project_menu(menu,project))
        self.tree.setItemWidget(project,1,more)
        if select:
            self.new_chat(project)
        return project

    def open_folder(self, item, _column):
        if item.parent() is None:
            directory = item.data(0,Qt.ItemDataRole.UserRole)
            if directory and not QDesktopServices.openUrl(QUrl.fromLocalFile(directory)):
                self.show_failure('无法打开项目文件夹')

    def create_desktop_project(self):
        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        try:
            if not location:
                raise FileNotFoundError
            desktop = Path(location).resolve(strict=True)
            for number in count(1):
                name = 'Ronova 新聊天' if number == 1 else f'Ronova 新聊天 ({number})'
                path = desktop/name
                if path in self.projects:
                    continue
                try:
                    path.mkdir()
                except FileExistsError:
                    continue
                break
        except OSError:
            self.show_failure('无法在桌面新建项目文件夹，请检查桌面目录是否可写，或手动选择项目文件夹。')
            return None
        project = self.add_project(path,select=False)
        self.project_names[path] = path.name
        self.save_project_preferences()
        return project

    def new_chat(self, project):
        if project is not None and project is self.deleting_project:
            return
        if project is None or self.project_path(project) is None:
            project = self.create_desktop_project()
            if project is None:
                return
        chat = self.add_chat(project)
        self.tree.setCurrentItem(chat.item)
        self.input.setFocus()

    def move_draft_to_project(self, chat, project):
        item = chat.item
        previous = item.parent()
        with QSignalBlocker(self.tree):
            previous.removeChild(item)
            project.insertChild(0,item)
            more = self.sidebar_menu('聊天操作',lambda menu: self.chat_menu(menu,chat))
            self.tree.setItemWidget(item,1,more)
            project.setExpanded(True)
            self.tree.setCurrentItem(item)

    def add_chat(self, project, title='新聊天', thread_id=None):
        item = QTreeWidgetItem([self.short_title(title)])
        item.setToolTip(0,title)
        key = uuid.uuid4().hex
        item.setData(0,Qt.ItemDataRole.UserRole,key)
        chat = WorkspaceChat(item,thread_id=thread_id,history_loaded=thread_id is None)
        self.chats[key] = chat
        if thread_id:
            self.codex_chats[thread_id] = chat
            project.addChild(item)
        else:
            project.insertChild(0,item)
        more = self.sidebar_menu('聊天操作',lambda menu: self.chat_menu(menu,chat))
        self.tree.setItemWidget(item,1,more)
        project.setExpanded(True)
        return chat

    @staticmethod
    def short_title(title):
        return title if len(title)<=24 else title[:24]+'…'

    def select_chat(self, item, previous=None):
        if item is not None and item.parent() is None:
            child = item.child(0)
            if self.active_chat is not None and self.active_chat.item.parent() is item:
                child = self.active_chat.item
            if child is not None:
                item.setExpanded(True)
                self.tree.setCurrentItem(child)
                return
        chat = self.chats.get(item.data(0,Qt.ItemDataRole.UserRole)) if item is not None else None
        if self.active_chat is chat:
            return
        if self.active_chat is not None:
            self.active_chat.draft = self.input.toPlainText()
        self.close_editor()
        self.redraw.stop()
        if self.playing_turn is not None:
            self.stop_playback()
        self.active_chat = chat
        self.input.setPlainText(chat.draft if chat else '')
        self.chat_title.setText(item.text(0) if chat else '选择或新建聊天')
        self.chat_title.setToolTip(item.toolTip(0) if chat else '')
        self.render_attachments()
        self.refresh_controls()
        self.refresh_alert()
        self.render_history(force_bottom=True)
        self.load_codex_history(chat)

    def chat_busy(self, chat):
        return bool(chat and (chat.request or chat.compacting or chat.deleting or chat.renaming
                              or chat.loading_history or chat.item.parent() is self.deleting_project))

    def set_chat_title(self, chat, title):
        label = self.short_title(title)
        chat.item.setText(0,label)
        chat.item.setToolTip(0,title)
        if chat is self.active_chat:
            self.chat_title.setText(label)
            self.chat_title.setToolTip(title)

    def rename_chat(self, chat):
        if self.chat_busy(chat) or chat.server_deleted:
            return
        previous = chat.item.toolTip(0)
        value, accepted = QInputDialog.getText(self,'重命名聊天','聊天名称',text=previous)
        title = ' '.join(value.split())
        if (not accepted or not title or title == previous or self.chat_busy(chat)
                or chat.server_deleted or chat.item.treeWidget() is not self.tree):
            return
        if not chat.thread_id:
            chat.pending_name = title
            self.set_chat_title(chat,title)
            return
        chat.renaming = True
        self.refresh_controls()
        self.codex.call('thread/name/set',{'threadId':chat.thread_id,'name':title},
                        lambda _result,error: self.chat_renamed(chat,title,error))

    def chat_renamed(self, chat, title, error):
        chat.renaming = False
        if chat.item.treeWidget() is not self.tree:
            return
        if error:
            chat.alert = f'无法重命名聊天：{error}'
        else:
            chat.pending_name = ''
            self.set_chat_title(chat,title)
        if chat is self.active_chat:
            self.refresh_controls()
            self.refresh_alert()

    def chat_menu(self, menu, chat):
        rename = menu.addAction(self.rename_icon,'重命名聊天')
        rename.setIconVisibleInMenu(True)
        rename.setEnabled(not self.chat_busy(chat) and not chat.server_deleted)
        rename.triggered.connect(lambda: self.rename_chat(chat))
        remove = menu.addAction(self.delete_icon,'删除聊天')
        remove.setIconVisibleInMenu(True)
        remove.setEnabled(not self.chat_busy(chat) and not self.deletion_in_progress())
        remove.triggered.connect(lambda: self.delete_chat(chat))

    def delete_selected_chat(self):
        if self.active_chat is not None:
            self.delete_chat(self.active_chat)
        elif self.tree.currentItem() is not None and self.tree.currentItem().parent() is None:
            self.delete_project(self.tree.currentItem())

    def project_menu(self, menu, project):
        rename = menu.addAction(self.rename_icon,'重命名项目')
        rename.setIconVisibleInMenu(True)
        rename.setEnabled(project is not self.deleting_project)
        rename.triggered.connect(lambda: self.rename_project(project))
        remove = menu.addAction(self.delete_icon,'删除项目')
        remove.setIconVisibleInMenu(True)
        remove.setEnabled(not self.deletion_in_progress() and not any(self.chat_busy(chat) for chat in self.project_chats(project)))
        remove.triggered.connect(lambda: self.delete_project(project))

    @staticmethod
    def project_path(project):
        path = project.data(0,Qt.ItemDataRole.UserRole)
        return Path(path) if path else None

    def rename_project(self, project):
        if project is self.deleting_project:
            return
        previous = project.text(0)
        dialog = ProjectDialog(self, '重命名项目', '确定')
        field = QLineEdit(previous)
        field.setAccessibleName('项目名称')
        label = QLabel('项目名称')
        label.setBuddy(field)
        dialog.body.addWidget(label)
        dialog.body.addWidget(field)
        dialog.confirm.setDefault(True)
        field.selectAll()
        field.setFocus()
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        value = field.text()
        dialog.deleteLater()
        name = ' '.join(value.split())
        if (not accepted or not name or name == previous or project is self.deleting_project
                or project.treeWidget() is not self.tree):
            return
        self.project_names[self.project_path(project)] = name
        project.setText(0,name)
        self.save_project_preferences()

    def deletion_in_progress(self):
        # A legacy screenshot can be referenced by two saved chats.
        return self.deleting_project is not None or any(chat.deleting for chat in self.chats.values())

    def project_chats(self, project):
        return [self.chats[project.child(index).data(0,Qt.ItemDataRole.UserRole)]
                for index in range(project.childCount())]

    def delete_project(self, project):
        if self.deletion_in_progress() or any(self.chat_busy(chat) for chat in self.project_chats(project)):
            return
        dialog = ProjectDialog(self, '删除项目', '删除')
        for text in (f'从加班区删除「{project.text(0)}」？',
                     '项目下的聊天和相关缓存将永久删除。磁盘上的原项目目录和文件会保留。'):
            label = QLabel(text)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            dialog.body.addWidget(label)
        dialog.cancel.setDefault(True)
        dialog.cancel.setFocus()
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if not accepted:
            return
        if (project.treeWidget() is not self.tree or self.deletion_in_progress()
                or any(self.chat_busy(chat) for chat in self.project_chats(project))):
            return
        self.deleting_project = project
        self.refresh_controls()
        if not self.codex.connected:
            self.delete_project_chats(project)
            return
        self.list_threads(lambda thread: self.import_project_thread(project,thread),
                          lambda error: self.project_threads_ready(project,error),cwd=str(self.project_cwd(project)))

    def import_project_thread(self, project, thread):
        if Path(thread['cwd']) == self.project_cwd(project):
            self.import_thread(thread,project)

    def project_threads_ready(self, project, error):
        if error:
            self.finish_project_deletion(project,error)
        else:
            self.list_threads(lambda thread: self.import_project_thread(project,thread),
                              lambda error: self.finish_project_deletion(project,error) if error
                              else self.delete_project_chats(project),cwd=str(self.project_cwd(project)),archived=True)

    def delete_project_chats(self, project):
        chats = self.project_chats(project)
        if chats:
            self.delete_chat_data(chats[0],lambda error: self.project_chat_deleted(project,error))
        else:
            self.finish_project_deletion(project)

    def project_chat_deleted(self, project, error):
        if error:
            self.finish_project_deletion(project,error)
        else:
            QTimer.singleShot(0,self,lambda: self.delete_project_chats(project))

    def finish_project_deletion(self, project, error=''):
        self.deleting_project = None
        if error:
            self.refresh_controls()
            self.load_codex_history(self.active_chat)
            box = QMessageBox(QMessageBox.Icon.Warning,'项目未完全删除',error,parent=self)
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.exec()
            return
        path = self.project_path(project)
        del self.projects[path]
        if self.tree.currentItem() is project:
            self.tree.setCurrentItem(self.projects.get(None))
        self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(project))
        alias = self.project_names.pop(path,None)
        if path is None:
            self.unassigned_hidden = True
        if alias is not None or path is None:
            self.save_project_preferences()
        self.refresh_controls()

    def confirm_deletion(self, title, question, detail):
        box = QMessageBox(QMessageBox.Icon.Warning,title,question,parent=self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setInformativeText(detail)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setText('删除')
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def delete_chat(self, chat):
        if self.chat_busy(chat) or self.deletion_in_progress():
            return
        if chat.thread_id or chat.draft or chat.pictures or chat.turns or (chat is self.active_chat and self.input.toPlainText()):
            if not self.confirm_deletion('删除聊天','确定删除这个聊天？','这条聊天记录和相关缓存将永久删除。'):
                return
        if chat.item.treeWidget() is self.tree and not self.chat_busy(chat) and not self.deletion_in_progress():
            self.delete_chat_data(chat)

    def delete_chat_data(self, chat, completed=None):
        if not chat.thread_id:
            self.remove_chat(chat)
            if completed:
                completed('')
            return
        chat.deleting = True
        self.refresh_controls()
        if chat.server_deleted:
            self.clean_deleted_chat(chat,completed)
            return
        prefix = self.media_prefix(chat.thread_id)
        try:
            media = self.codex.media
            chat.delete_files = {path for path in media.glob(prefix+'*') if path.suffix in ('.png','.tmp')}
        except OSError:
            self.chat_deleted(chat,'无法读取本地截图缓存。',completed)
            return
        legacy = {path for turn in chat.turns for picture in turn.pictures
                  if (path := self.legacy_media(self.codex.media / (picture.key+'.png'))) is not None}
        self.list_turn_media(chat.thread_id,legacy,
                             lambda error: self.delete_media_ready(chat,legacy,completed,error))

    def delete_media_ready(self, chat, legacy, completed, error):
        if error:
            self.chat_deleted(chat,error,completed)
        elif legacy:
            self.exclude_shared_media(chat.thread_id,legacy,
                                      lambda error: self.delete_thread(chat,legacy,completed,error))
        else:
            self.delete_thread(chat,legacy,completed,'')

    def delete_thread(self, chat, legacy, completed, error):
        if error:
            self.chat_deleted(chat,error,completed)
            return
        chat.delete_files.update(legacy)
        chat.delete_files.update(path.with_suffix('.tmp') for path in legacy)
        self.codex.call('thread/delete',{'threadId':chat.thread_id},
                        lambda _result,error: self.thread_deleted(chat,completed,error))

    def thread_deleted(self, chat, completed, error):
        if error and not chat.server_deleted:
            self.chat_deleted(chat,error,completed)
            return
        chat.server_deleted = True
        self.clean_deleted_chat(chat,completed)

    def clean_deleted_chat(self, chat, completed):
        try:
            if chat.delete_files:
                media = self.codex.media
                if media.resolve() != media:
                    raise OSError
                journal = media/(self.media_prefix(chat.thread_id)+'cleanup.json')
                temporary = journal.with_suffix('.tmp')
                temporary.unlink(missing_ok=True)
                with temporary.open('x',encoding='utf-8') as stream:
                    json.dump(sorted(path.name for path in chat.delete_files),stream)
                temporary.replace(journal)
                self.clear_media_journal(journal)
        except OSError:
            self.chat_deleted(chat,'聊天记录已删除，本地截图缓存未清理完。可再次选择删除重试。',completed)
            return
        self.chat_deleted(chat,'',completed)

    def clear_media_journal(self, journal):
        prefix = journal.name.removesuffix('cleanup.json')
        for name in json.loads(journal.read_text(encoding='utf-8')):
            path = self.codex.media/name
            if (path.parent == self.codex.media and path.suffix in ('.png','.tmp')
                    and (name.startswith(prefix) or self.legacy_media(path.with_suffix('.png')))):
                path.unlink(missing_ok=True)
        journal.unlink(missing_ok=True)

    def recover_media_cleanup(self):
        media = self.codex.media
        if media.resolve() != media:
            return
        for journal in media.glob('*-cleanup.json'):
            prefix = journal.name.removesuffix('-cleanup.json')
            if journal.is_symlink() or len(prefix) != 32 or any(char not in '0123456789abcdef' for char in prefix):
                continue
            try:
                self.clear_media_journal(journal)
            except (OSError, ValueError, TypeError):
                continue

    def chat_deleted(self, chat, error, completed):
        chat.deleting = False
        if not error:
            self.remove_chat(chat)
        elif chat.item.treeWidget() is not None:
            chat.alert = f'无法删除聊天：{error}'
            chat.failed_turn = None
            if chat is self.active_chat:
                self.refresh_alert()
                self.refresh_controls()
        if completed:
            completed(error)

    def remove_chat(self, chat):
        item = chat.item
        key = item.data(0,Qt.ItemDataRole.UserRole)
        if self.chats.pop(key,None) is None:
            return
        if chat.thread_id:
            self.deleted_threads.add(chat.thread_id)
            self.codex_chats.pop(chat.thread_id,None)
            self.codex.loaded.discard(chat.thread_id)
        project = item.parent()
        index = project.indexOfChild(item)
        was_active = chat is self.active_chat
        if was_active:
            with QSignalBlocker(self.tree):
                project.takeChild(index)
                count = project.childCount()
                target = project.child(min(index,count-1)) if count else project
                self.tree.setCurrentItem(target)
            self.select_chat(target)
        else:
            project.takeChild(index)
        chat.turns.clear()
        chat.pictures = ()
        chat.draft = ''
        chat.failed_turn = None
        chat.delete_files.clear()

    def codex_account_changed(self):
        self.refresh_controls()
        self.load_catalog()

    def load_catalog(self, cursor=None):
        if not self.codex.connected or not self.codex.signed_in:
            return
        if cursor is None:
            if self.catalog_state in ('loading','ready'):
                return
            self.catalog_state = 'loading'
            self.catalog.clear()
        params = {'limit':40,'includeHidden':False}
        if cursor:
            params['cursor'] = cursor
        self.codex.call('model/list',params,lambda result,error: self.catalog_loaded(result,error,cursor))

    def catalog_loaded(self, result, error, cursor):
        if error:
            self.catalog.clear()
            self.catalog_state = 'error'
        else:
            for model in result.get('data',[]):
                if model.get('model') and not model.get('hidden'):
                    self.catalog[model['model']] = model
            next_cursor = result.get('nextCursor')
            if next_cursor and next_cursor != cursor:
                self.load_catalog(next_cursor)
                return
            self.catalog_state = 'ready'
        self.refresh_controls()
        menu = self.model_button.menu()
        if menu.isVisible() and menu.actions():
            if error:
                menu.actions()[0].setText('无法读取可用模型，请重新打开菜单')
            else:
                menu.clear()
                self.model_menu(menu)

    def catalog_model(self, chat):
        if chat.model:
            return self.catalog.get(chat.model,{})
        return next((model for model in self.catalog.values() if model.get('isDefault')),{})

    def permission_menu(self, menu):
        chat = self.active_chat
        if chat is None:
            return
        picker = PermissionPicker(PERMISSIONS,chat.permission,self.approval_icons,
                                  lambda mode: self.set_permission(chat,mode),menu.close)
        mount_panel(menu,picker)

    def set_permission(self, chat, mode):
        if chat is self.active_chat and not self.chat_busy(chat):
            chat.permission = mode
            self.refresh_controls()

    def model_menu(self, menu):
        chat = self.active_chat
        if chat is None:
            return
        if self.catalog_state == 'loading' or not self.catalog:
            self.load_catalog()
            message = '请先登录 ChatGPT' if not self.codex.signed_in else '正在读取可用模型…'
            if self.catalog_state == 'ready':
                message = 'Codex 未返回可选模型'
            menu.addAction(message).setEnabled(False)
            return
        current = self.catalog_model(chat)
        picker = ModelPicker(self.catalog, chat.model or current.get('model'), chat.effort, EFFORT_NAMES,
                             lambda model, effort: self.set_model(chat, model, effort),
                             self.model_button.screen().availableGeometry())
        mount_panel(menu,picker)
        if menu.isVisible():
            picker.fit_menu()

    def set_model(self, chat, model, effort=None):
        if chat is not self.active_chat or self.chat_busy(chat):
            return
        previous = chat.model or self.catalog_model(chat).get('model')
        chat.model = model
        chat.effort = effort or self.catalog_model(chat).get('defaultReasoningEffort')
        if model != previous:
            chat.usage = None
        self.refresh_controls()

    @staticmethod
    def usage_label(usage):
        if usage is None:
            return '发送消息后显示上下文用量'
        used,limit = usage
        if not limit:
            return f'已用 {used:,} tokens'
        return f'已用 {used:,} / {limit:,} tokens（{usage_percent(used,limit)}）'

    def context_menu(self, menu):
        chat = self.active_chat
        menu.addSection('上下文容量')
        menu.addAction(self.usage_label(chat.usage if chat else None)).setEnabled(False)
        self.add_compact_action(menu, chat)

    def composer_menu(self, menu):
        chat = self.active_chat
        self.add_compact_action(menu, chat)
        plan = menu.addAction(self.plan_icon, '计划模式')
        plan.setIconVisibleInMenu(True)
        plan.setCheckable(True)
        plan.setChecked(bool(chat and chat.plan_mode))
        plan.setEnabled(bool(chat and not chat.server_deleted and not self.chat_busy(chat)))
        plan.triggered.connect(lambda enabled: self.set_plan_mode(chat, enabled))

    def add_compact_action(self, menu, chat):
        compact = menu.addAction(self.compact_icon, '压缩上下文')
        compact.setIconVisibleInMenu(True)
        compact.setEnabled(self.can_compact(chat))
        compact.triggered.connect(lambda: self.compact_context(chat))

    def can_compact(self, chat):
        return bool(chat and chat.thread_id and self.codex.signed_in and not chat.server_deleted
                    and not self.chat_busy(chat))

    def set_plan_mode(self, chat, enabled):
        if chat is self.active_chat and chat is not None and not chat.server_deleted and not self.chat_busy(chat):
            chat.plan_mode = enabled
            self.refresh_controls()

    def toggle_dictation(self):
        on = self.mic.isChecked()
        if self.voice is None:
            self.mic.setChecked(False)
            self.status.setText('语音未配置，可继续用文字提问')
            return
        try:
            if on:
                self.voice.start_dictation()
                self.mic.setIcon(self.mic_icon_active)
                self.voice_note = '聆听中 · 停顿约1秒自动转写'
            else:
                self.voice.stop_dictation()
                self.mic.setIcon(self.mic_icon)
                self.voice_note = ''
        except WorkspaceVoiceBusy as exc:
            self.mic.setChecked(False); self.voice_note = ''
            self.status.setText(str(exc))
            return
        except Exception as exc:
            self.mic.setChecked(False); self.mic.setIcon(self.mic_icon); self.voice_note = ''
            self.status.setText(f'收音失败（{type(exc).__name__}），文字可继续')
            return
        self.refresh_controls()

    def dictation_changed(self, active):
        self.mic.setChecked(bool(active))
        self.mic.setIcon(self.mic_icon_active if active else self.mic_icon)
        self.mic.setProperty('listening', bool(active))
        self.mic.style().unpolish(self.mic); self.mic.style().polish(self.mic)
        self.refresh_controls()

    def show_partial_transcript(self, text):
        if not self.mic.isChecked():
            return
        text = ' '.join(str(text).split())
        if len(text) > 24:
            text = '…' + text[-23:]
        self.voice_note = '识别中：' + text
        self.refresh_controls()

    def show_voice_status(self, text):
        self.voice_note = ' '.join(str(text).split())
        self.refresh_controls()

    def show_voice_preparation(self, text):
        self.input.set_voice_preparation(text)
        if text:
            self.show_voice_status('')

    def reply_playback_changed(self, playing):
        if not playing:
            self.set_playing_turn(None)
            self.show_voice_status('')

    def receive_transcript(self, text):
        """Recognised speech lands in the composer; sending still needs a click."""
        text = ' '.join(str(text).split())
        if not text:
            return
        current = self.input.toPlainText()
        joined = f'{current.rstrip()} {text}' if current.strip() else text
        self.input.setPlainText(joined)
        cursor = self.input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.input.setTextCursor(cursor)
        self.input.setFocus()
        self.voice_note = '转写已填入，可修改后发送（不会自动发送）'
        self.refresh_controls()

    def play_reply(self, turn, text):
        if self.voice is None or turn.state != 'done' or turn not in self.history.rows:
            return
        if self.playing_turn is turn:
            self.stop_playback()
            return
        try:
            if self.voice.speak_answer(text):
                self.set_playing_turn(turn)
        except WorkspaceVoiceBusy as exc:
            self.show_voice_status(str(exc))

    def set_playing_turn(self, turn):
        previous = self.history.rows.get(self.playing_turn)
        if previous is not None:
            previous.set_playing(False)
        self.playing_turn = turn
        current = self.history.rows.get(turn)
        if current is not None:
            current.set_playing(True)

    def stop_playback(self):
        if self.voice is not None:
            self.voice.stop_playback()
        self.voice_note = ''

    def refresh_controls(self):
        chat = self.active_chat
        request = self.request
        active = request is not None
        busy = self.chat_busy(chat) or bool(chat and chat.server_deleted)
        self.input.setEnabled(chat is not None and not chat.deleting and not chat.server_deleted
                              and chat.item.parent() is not self.deleting_project)
        self.send.setVisible(not active)
        self.send.setEnabled(bool(chat and not busy and (self.input.toPlainText().strip() or self.pictures)))
        self.stop.setVisible(active)
        stopping = active and request.stopping
        self.stop.setEnabled(not stopping)
        planning = bool(chat and chat.plan_mode)
        self.add_action.setEnabled(bool(chat and not busy))
        if self.add_action.property('planMode') != planning:
            self.add_action.setProperty('planMode', planning)
            self.add_action.setIcon(self.composer_icons[planning])
            self.add_action.setToolTip('更多操作 · 计划模式已开启' if planning else '更多操作')
            self.add_action.style().unpolish(self.add_action)
            self.add_action.style().polish(self.add_action)
            self.add_action.update()
        status = ''
        if chat and chat.loading_history:
            status = '读取历史…'
        elif chat and chat.compacting:
            status = '正在压缩上下文…'
        elif chat and (chat.deleting or chat.item.parent() is self.deleting_project):
            status = '正在删除…'
        elif chat and chat.renaming:
            status = '正在重命名…'
        if active:
            status = '正在停止…' if stopping else ('正在制定计划…' if request.plan_mode else '正在回答…')
        elif planning and not status:
            status = '计划模式'
        if self.voice_note:
            status = f'{status} · {self.voice_note}' if status else self.voice_note
        if len(status) > 44:
            status = status[:43] + '…'
        self.status.setText(status)
        self.status.setToolTip(self.status.text())
        mic = getattr(self, 'mic', None)
        if mic is not None:
            mic.setEnabled(bool(chat) and not busy and not chat.deleting
                           and not chat.server_deleted)
        self.permission_button.setEnabled(chat is not None and not busy)
        mode = chat.permission if chat else 'manual'
        self.permission_button.setText(PERMISSIONS[mode][0] + ' ▼')
        self.permission_button.setIcon(self.approval_icons[mode])
        self.model_button.setEnabled(chat is not None and not busy)
        model = self.catalog_model(chat) if chat else {}
        name = model.get('displayName') or (chat.model if chat else None) or 'Codex'
        effort = (chat.effort if chat else None) or model.get('defaultReasoningEffort')
        label = f'{name} · {EFFORT_NAMES.get(effort,effort)}' if effort else name
        self.model_button.setText(self.short_title(label) + ' ▼')
        self.model_button.setToolTip(label)
        usage = chat.usage if chat else None
        self.context_button.show_usage(usage)
        self.account_banner.setVisible(not self.codex.signed_in)
        self.account_text.setText(self.codex.message or ('正在连接 Codex…' if self.codex.connecting else
                                                        '使用 ChatGPT 账号登录 Codex'))
        self.refresh_approval()

    def show_failure(self, message, turn=None):
        if self.active_chat is None:
            self.alert_text.setText(message)
            self.retry.hide()
            self.alert.show()
            return
        self.active_chat.failed_turn = turn
        self.active_chat.alert = message
        self.refresh_alert()

    def dismiss_alert(self):
        if self.active_chat is not None:
            self.active_chat.alert = ''
        self.refresh_alert()

    def refresh_alert(self):
        chat = self.active_chat
        self.alert_text.setText(chat.alert if chat else '')
        self.retry.setVisible(bool(chat and chat.failed_turn is not None))
        self.alert.setVisible(bool(chat and chat.alert))

    def attach_image(self, image):
        self.attach_images((image,))

    def attach_images(self, images):
        images = tuple(images)
        if any(image.isNull() for image in images):
            self.show_failure('无法读取图片，请重新截图或粘贴')
            return
        pictures = []
        for image in images:
            if max(image.width(),image.height())>4096:
                image = image.scaled(4096,4096,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
            image = image.convertToFormat(QImage.Format.Format_ARGB32)
            image.setDevicePixelRatio(1.)
            pictures.append(Picture(image))
        if pictures:
            self.set_pictures((*self.pictures, *pictures), scroll_to_end=True)

    def add_picture(self, picture):
        if all(attached.key != picture.key for attached in self.pictures):
            self.set_pictures((*self.pictures, picture), scroll_to_end=True)

    def set_pictures(self, pictures, *, scroll_to_end=False):
        chat = self.active_chat
        if chat and (chat.deleting or chat.server_deleted or chat.item.parent() is self.deleting_project):
            return
        self.close_editor()
        if self.active_chat is None and pictures:
            self.new_chat(self.current_project())
            if self.active_chat is None:
                return
        if self.active_chat is not None:
            self.active_chat.pictures = pictures
        self.render_attachments()
        self.refresh_controls()
        if scroll_to_end and pictures:
            preview = self.attachment_row.itemAt(self.attachment_row.count() - 1).widget()
            QTimer.singleShot(0, preview, lambda: self.attachment.ensureWidgetVisible(preview, 8, 0)
                              if not preview.isHidden() else None)

    def render_attachments(self):
        while self.attachment_row.count():
            preview = self.attachment_row.takeAt(0).widget()
            preview.hide(); preview.deleteLater()
        self.attachment.setVisible(bool(self.pictures))
        preview_height = 0
        for picture in self.pictures:
            preview = PicturePreview()
            preview.set_image(picture.preview)
            preview.setAccessibleName('放大并标记待发送图片')
            preview.setToolTip('双击放大并标记')
            preview.double_clicked.connect(lambda selected=picture: self.open_editor(selected))
            self.attachment_row.addWidget(preview)
            preview_height = max(preview_height, preview.height())
            for key in ('Return', 'Enter', 'Space'):
                shortcut = QShortcut(QKeySequence(key), preview)
                shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
                shortcut.activated.connect(lambda selected=picture: self.open_editor(selected))
            remove = CloseButton(preview, color=COLORS['surface'])
            remove.setObjectName('workRemoveImage')
            remove.setAccessibleName('移除待发送图片')
            remove.setToolTip('移除图片')
            remove.setCursor(Qt.CursorShape.PointingHandCursor)
            remove.setFixedSize(18,18)
            remove.move(preview.width() - remove.width() - 2, 2)
            remove.clicked.connect(lambda checked=False, selected=picture: self.set_pictures(
                tuple(attached for attached in self.pictures if attached is not selected)))
            preview.show()
        if self.pictures:
            margins = self.attachment_row.contentsMargins()
            scrollbar_height = self.attachment.horizontalScrollBar().sizeHint().height()
            self.attachment.setFixedHeight(preview_height + margins.top() + margins.bottom() + scrollbar_height)

    def open_editor(self, picture):
        if picture not in self.pictures: return
        self.editing_picture = picture
        self.sidebar.setEnabled(False)
        self.pages.setCurrentWidget(self.editor)
        self.editor.open(picture.source,picture.marks)

    def close_editor(self):
        self.editing_picture = None
        if self.pages.currentWidget() is self.editor:
            self.pages.setCurrentWidget(self.chat)
            self.editor.canvas.load(None,())
            self.sidebar.setEnabled(True)

    def save_marks(self, marks):
        picture = self.editing_picture
        if picture is not None and marks!=picture.marks:
            edited = Picture(picture.source,marks)
            self.set_pictures(tuple(edited if attached is picture else attached for attached in self.pictures))
        else:
            self.close_editor()

    def submit(self):
        question = self.input.toPlainText().strip()
        if not question and self.pictures:
            question = '请查看这些截图。' if len(self.pictures)>1 else '请查看这张截图。'
        if not question or self.active_chat is None or self.chat_busy(self.active_chat) or self.active_chat.server_deleted:
            return
        if len(question)>4000:
            self.show_failure('请输入 1–4000 字的问题')
            return
        if not self.start_codex(WorkspaceTurn(question,self.pictures)): return
        self.stop_playback()
        self.input.clear()

    def retry_request(self):
        chat = self.active_chat
        if chat and chat.failed_turn is not None and not self.chat_busy(chat) and not chat.server_deleted:
            self.start_codex(chat.failed_turn)

    def begin_turn(self, request):
        chat, turn = request.chat, request.turn
        if turn not in chat.turns:
            if not chat.turns and chat.thread_id is None and not chat.pending_name:
                title = ' '.join(turn.question.split())
                self.set_chat_title(chat,title)
            chat.turns.append(turn)
            del chat.turns[:-24]
        turn.state = 'pending'
        turn.items.clear()
        turn.codex_id = None
        chat.request = request
        chat.failed_turn = None
        self.dismiss_alert()
        self.refresh_controls(); self.render_history(force_bottom=True)

    def load_codex_threads(self, cursor=None):
        params = {'limit':50, 'sourceKinds':['appServer','vscode','cli'], 'modelProviders':[]}
        if cursor:
            params['cursor'] = cursor
        self.codex.call('thread/list',params,lambda result,error: self.codex_threads_loaded(result,error,cursor))
        if cursor is None:
            self.load_codex_history(self.active_chat)

    def codex_threads_loaded(self, result, error, cursor):
        if error:
            if self.codex.connected and self.active_chat and not self.active_chat.alert:
                self.show_failure(f'无法读取 Codex 会话列表：{error}')
            return
        for thread in result.get('data',[]):
            self.import_thread(thread)
        next_cursor = result.get('nextCursor')
        if next_cursor and next_cursor != cursor:
            self.load_codex_threads(next_cursor)

    def import_thread(self, thread, project=None):
        thread_id = thread['id']
        if thread_id in self.codex_chats or thread_id in self.deleted_threads or thread.get('parentThreadId'):
            return
        if project is None:
            cwd = Path(thread['cwd'])
            project = self.add_project(None if cwd == self.codex.default_cwd else cwd,select=False)
        title = ' '.join((thread.get('name') or thread.get('preview') or 'Codex 聊天').split())
        chat = self.add_chat(project,title,thread_id)
        chat.model = thread.get('model')
        chat.effort = thread.get('reasoningEffort')

    def read_pages(self, method, params, consume, finished):
        cursors = set()

        def received(result, error):
            if error:
                finished(error)
                return
            consume(result.get('data',[]))
            cursor = result.get('nextCursor')
            if not cursor:
                finished('')
            elif cursor in cursors:
                finished('Codex 返回了重复的分页游标，请重试。')
            else:
                cursors.add(cursor)
                self.codex.call(method,{**params,'cursor':cursor},received)

        self.codex.call(method,params,received)

    def list_threads(self, visit, finished, *, cwd=None, archived=False):
        params = {'limit':50,'modelProviders':[],'archived':archived}
        if cwd is not None:
            params['cwd'] = cwd

        def consume(threads):
            for thread in threads:
                visit(thread)

        self.read_pages('thread/list',params,consume,finished)

    @staticmethod
    def media_prefix(thread_id):
        return sha256(thread_id.encode('utf-8')).hexdigest()[:32] + '-'

    def legacy_media(self, filename):
        path = Path(filename)
        # Before filenames were scoped to a thread, the app used bare UUIDs.
        if (path.parent == self.codex.media and path.suffix == '.png' and len(path.stem) == 32
                and all(char in '0123456789abcdef' for char in path.stem)):
            return path
        return None

    def list_turn_media(self, thread_id, paths, finished, *, exclude=False):
        def consume(turns):
            for turn in turns:
                for item in turn.get('items',[]):
                    if item.get('type') != 'userMessage':
                        continue
                    for value in item.get('content',[]):
                        if value.get('type') == 'localImage' and value.get('path'):
                            path = self.legacy_media(value['path'])
                            if path is not None:
                                if exclude:
                                    paths.discard(path)
                                else:
                                    paths.add(path)

        self.read_pages('thread/turns/list',{'threadId':thread_id,'limit':24,'itemsView':'full',
                                              'sortDirection':'desc'},consume,finished)

    def exclude_shared_media(self, thread_id, paths, finished):
        others = set()

        def visit(thread):
            if thread['id'] != thread_id:
                others.add(thread['id'])

        def scan_next(error):
            if error or not paths or not others:
                finished(error)
            else:
                self.list_turn_media(others.pop(),paths,scan_next,exclude=True)

        def active_listed(error):
            if error:
                finished(error)
            else:
                self.list_threads(visit,scan_next,archived=True)

        self.list_threads(visit,active_listed)

    def load_codex_history(self, chat):
        if (chat is None or not self.codex.connected or chat.history_loaded or chat.loading_history
                or chat.deleting or chat.server_deleted or chat.item.parent() is self.deleting_project):
            return
        chat.loading_history = True
        self.refresh_controls()
        self.codex.call('thread/turns/list',
                        {'threadId':chat.thread_id,'limit':24,'sortDirection':'desc','itemsView':'full'},
                        lambda result,error: self.codex_history_loaded(chat,result,error))

    def codex_history_loaded(self, chat, result, error):
        chat.loading_history = False
        if error:
            chat.alert = f'无法读取 Codex 聊天记录：{error}'
        else:
            chat.history_loaded = True
            restored = []
            current_ids = {turn.codex_id for turn in chat.turns if turn.codex_id}
            for raw in reversed(result.get('data',[])):
                if raw.get('id') in current_ids:
                    continue
                items = raw.get('items',[])
                inputs = [value for item in items if item.get('type') == 'userMessage'
                          for value in item.get('content',[])]
                question = '\n'.join(value.get('text','') for value in inputs if value.get('type') == 'text')
                pictures = tuple(picture for value in inputs if value.get('type') == 'localImage' and value.get('path')
                                 if (picture := self.restore_picture(value['path'])) is not None)
                turn = WorkspaceTurn(question,pictures,state=self.codex_turn_state(raw),codex_id=raw.get('id'))
                turn.items = {item['id']:item for item in items if item.get('type') in CODEX_ITEMS}
                restored.append(turn)
            chat.turns = (restored + chat.turns)[-24:]
        if chat is self.active_chat:
            self.refresh_alert()
            self.refresh_controls()
            self.render_history(force_bottom=True)

    def restore_picture(self, filename):
        path = Path(filename)
        try:
            if path.resolve().parent != self.codex.media or path.suffix != '.png':
                return None
            info = path.stat()
            if info.st_size > IMAGE_LIMIT:
                return None
        except OSError:
            return None
        image = QImage(str(path))
        return Picture(image,key=path.stem) if not image.isNull() else None

    def start_codex(self, turn):
        chat = self.active_chat
        retry = turn if turn in chat.turns else None
        if not self.codex.signed_in:
            self.codex.start()
            self.show_failure(self.codex.message or '请先在「设置 → 大模型」使用 ChatGPT 登录。',retry)
            return False
        project = chat.item.parent()
        needs_project = chat.thread_id is None and self.project_path(project) is None
        cwd = self.project_cwd(project)
        if not needs_project and not cwd.is_dir():
            self.show_failure('找不到这个项目文件夹，请确认目录仍然存在。',retry)
            return False
        if turn.pictures:
            model = self.catalog_model(chat)
            if 'image' not in model.get('inputModalities',['text','image']):
                self.show_failure('当前模型不支持图片，请选择支持图片的模型。',retry)
                return False
            try:
                for picture in turn.pictures:
                    _ = picture.png
            except ChatError as exc:
                self.show_failure(str(exc),retry)
                return False
        if needs_project:
            project = self.create_desktop_project()
            if project is None:
                return False
            self.move_draft_to_project(chat,project)
            cwd = self.project_cwd(project)
        run = CodexRun(chat,turn,cwd,self.turn_options(chat),plan_mode=chat.plan_mode)
        self.begin_turn(run)
        if chat.thread_id in self.codex.loaded:
            self.prepare_codex_turn(run)
        else:
            params = self.thread_options(chat,cwd,run.options)
            self.codex.call('thread/resume' if chat.thread_id else 'thread/start',params,
                            lambda result,error: self.codex_thread_ready(run,result,error))
        return True

    def chat_cwd(self, chat):
        return self.project_cwd(chat.item.parent())

    def project_cwd(self, project):
        return self.project_path(project) or self.codex.default_cwd

    def turn_options(self, chat):
        options = {'approvalPolicy':'never' if chat.permission == 'read' else 'on-request',
                   'approvalsReviewer':'auto_review' if chat.permission == 'review' else 'user'}
        if chat.model:
            options['model'] = chat.model
        if chat.effort:
            options['effort'] = chat.effort
        return options

    def thread_options(self, chat, cwd, options):
        params = {key:value for key,value in options.items() if key != 'effort'}
        writable = cwd != self.codex.default_cwd and options['approvalPolicy'] != 'never'
        params.update(cwd=str(cwd),sandbox='workspace-write' if writable else 'read-only')
        if chat.thread_id:
            params.update(threadId=chat.thread_id,excludeTurns=True)
        return params

    def compact_context(self, chat):
        if not self.can_compact(chat):
            return
        chat.compacting = True
        self.refresh_controls()
        if chat.thread_id in self.codex.loaded:
            self.send_compaction(chat)
        else:
            cwd = self.chat_cwd(chat)
            self.codex.call('thread/resume',self.thread_options(chat,cwd,self.turn_options(chat)),
                            lambda result,error: self.compact_resumed(chat,result,error))

    def compact_resumed(self, chat, result, error):
        if not chat.compacting:
            return
        if error:
            self.finish_compaction(chat,error)
            return
        self.record_collaboration_mode(chat, result)
        self.codex.loaded.add(chat.thread_id)
        self.send_compaction(chat)

    def send_compaction(self, chat):
        self.codex.call('thread/compact/start',{'threadId':chat.thread_id},
                        lambda _result,error: self.finish_compaction(chat,error) if error else None)

    def finish_compaction(self, chat, error=''):
        chat.compacting = False
        if error:
            chat.alert = f'无法压缩上下文：{error}'
            chat.failed_turn = None
        if chat is self.active_chat:
            self.refresh_alert()
            self.refresh_controls()

    def codex_thread_ready(self, run, result, error):
        if run.chat.request is not run:
            return
        thread_id = result.get('thread',{}).get('id')
        if error or not thread_id:
            self.finish_codex(run,'failed',error or 'Codex 未返回会话编号，请重试。')
            return
        run.chat.thread_id = thread_id
        if run.chat.model is None:
            run.chat.model = result.get('model')
        if run.chat.effort is None:
            run.chat.effort = result.get('reasoningEffort')
        self.record_collaboration_mode(run.chat, result)
        self.codex_chats[thread_id] = run.chat
        self.codex.loaded.add(thread_id)
        self.prepare_codex_turn(run)

    @staticmethod
    def record_collaboration_mode(chat, result):
        mode = (result.get('collaborationMode') or {}).get('mode')
        if mode in ('plan', 'default'):
            chat.codex_used_plan_mode = mode == 'plan'

    def prepare_codex_turn(self, run):
        if run.chat.pending_name:
            title = run.chat.pending_name
            self.codex.call('thread/name/set',{'threadId':run.chat.thread_id,'name':title},
                            lambda _result,error: self.initial_name_saved(run,title,error))
        else:
            self.send_codex_turn(run)

    def initial_name_saved(self, run, title, error):
        if run.chat.request is run:
            self.chat_renamed(run.chat,title,error)
            self.send_codex_turn(run)

    def send_codex_turn(self, run):
        turn = run.turn
        options = dict(run.options)
        if run.plan_mode or run.chat.codex_used_plan_mode:
            catalog_model = self.catalog_model(run.chat)
            model = options.get('model') or run.chat.model or catalog_model.get('model')
            if not model:
                self.finish_codex(run,'failed','暂时无法确定 Codex 模型，请选择一个模型后重试。')
                return
            effort = options.get('effort') or run.chat.effort or catalog_model.get('defaultReasoningEffort')
            options['collaborationMode'] = {'mode':'plan' if run.plan_mode else 'default',
                                            'settings':{'model':model,'reasoning_effort':effort,
                                                        'developer_instructions':None}}
        if run.plan_mode:
            options.update(approvalPolicy='never', approvalsReviewer='user')
        inputs = [{'type':'text','text':turn.question,'text_elements':[]}]
        try:
            for picture in turn.pictures:
                key = picture.key.rsplit('-',1)[-1]
                path = self.codex.media / (self.media_prefix(run.chat.thread_id) + key + '.png')
                temporary = path.with_suffix('.tmp')
                if not path.exists():
                    temporary.write_bytes(picture.png)
                    temporary.replace(path)
                inputs.append({'type':'localImage','path':str(path)})
        except OSError:
            self.finish_codex(run,'failed','无法保存要发送给 Codex 的截图，请重试。')
            return
        sandbox = {'type':'readOnly','networkAccess':False}
        if run.cwd != self.codex.default_cwd and options['approvalPolicy'] != 'never':
            sandbox.update(type='workspaceWrite',writableRoots=[str(run.cwd)],
                           excludeTmpdirEnvVar=True,excludeSlashTmp=True)
        run.turn_sent = True
        self.codex.call('turn/start',{'threadId':run.chat.thread_id,'cwd':str(run.cwd),'input':inputs,
                                    **options,'sandboxPolicy':sandbox},
                        lambda result,error: self.codex_turn_started(run,result,error))

    def codex_turn_started(self, run, result, error):
        if run.chat.request is not run:
            return
        turn_id = result.get('turn',{}).get('id')
        if error or not turn_id:
            self.finish_codex(run,'failed',error or 'Codex 未启动本轮请求。')
        elif run.turn_id is None:
            run.turn_id = turn_id
            run.turn.codex_id = turn_id
            run.chat.codex_used_plan_mode = run.plan_mode
            if run.stopping:
                self.interrupt_codex(run)

    @staticmethod
    def codex_turn_state(turn):
        return {'completed':'done','failed':'failed'}.get(turn.get('status'),'stopped')

    def codex_event(self, method, params):
        thread_id = params.get('threadId')
        if method == 'thread/closed':
            self.codex.loaded.discard(thread_id)
        chat = self.codex_chats.get(thread_id)
        if chat is None:
            return
        if method == 'thread/deleted':
            if chat.deleting:
                chat.server_deleted = True
            else:
                self.remove_chat(chat)
            return
        if method == 'thread/name/updated' and params.get('threadName'):
            self.set_chat_title(chat,params['threadName'])
            return
        if method == 'thread/tokenUsage/updated':
            self.update_context_usage(chat,params.get('tokenUsage') or {})
            return
        if chat.compacting and method == 'turn/completed':
            turn = params.get('turn',{})
            error = (turn.get('error') or {}).get('message','')
            if not error and turn.get('status') != 'completed':
                error = 'Codex 未完成本次压缩。'
            self.finish_compaction(chat,error)
            return
        run = chat.request
        if run is None or not run.turn_sent:
            return
        raw = params.get('turn') or {}
        if method == 'turn/started' and run.turn_id is None:
            self.codex_turn_started(run,params,'')
            return
        if method == 'serverRequest/resolved':
            request_id = params.get('requestId')
            run.approvals.pop(request_id,None)
            if run.question_dialog and run.question_dialog[0] == request_id:
                run.question_dialog[1].reject()
            self.update_approval_badge(run)
            return
        if run.turn_id is None or (params.get('turnId') or raw.get('id')) != run.turn_id:
            return
        if method == 'turn/completed':
            for item in raw.get('items',[]):
                self.update_codex_item(run,item)
            state = self.codex_turn_state(raw)
            error = (raw.get('error') or {}).get('message') or run.error or 'Codex 请求失败。'
            self.finish_codex(run,state,error if state == 'failed' else '')
            return
        if method == 'error' and not params.get('willRetry'):
            run.error = (params.get('error') or {}).get('message','')
        elif method in ('item/started','item/completed'):
            self.update_codex_item(run,params['item'])
        elif method == 'item/agentMessage/delta':
            self.append_codex_output(run,params,'agentMessage','text')
        elif method == 'item/plan/delta':
            self.append_codex_output(run,params,'plan','text')
        elif method == 'item/commandExecution/outputDelta':
            self.append_codex_output(run,params,'commandExecution','aggregatedOutput')
        if chat is self.active_chat and not self.redraw.isActive():
            self.redraw.start()

    def update_context_usage(self, chat, usage):
        used = (usage.get('last') or {}).get('totalTokens')
        limit = usage.get('modelContextWindow')
        if isinstance(used,int) and used >= 0:
            chat.usage = (used,limit if isinstance(limit,int) and limit > 0 else None)
            if chat is self.active_chat:
                self.context_button.show_usage(chat.usage)

    @staticmethod
    def update_codex_item(run, item):
        if item.get('type') not in CODEX_ITEMS:
            return
        existing = run.turn.items.setdefault(item['id'],{})
        existing.update({key:value for key,value in item.items() if value is not None})
        if 'aggregatedOutput' in existing:
            existing['aggregatedOutput'] = existing['aggregatedOutput'][-16000:]

    @staticmethod
    def append_codex_output(run, params, kind, field):
        item = run.turn.items.setdefault(params['itemId'],{'type':kind})
        limit = 16000 if kind == 'commandExecution' else 80000
        item[field] = (item.get(field,'') + params.get('delta',''))[-limit:]

    def codex_approval(self, request_id, method, params):
        chat = self.codex_chats.get(params.get('threadId'))
        run = chat.request if chat is not None else None
        valid = run is not None and run.turn_id is not None and run.turn_id == params.get('turnId') and not run.stopping
        if method == USER_INPUT_REQUEST:
            if not valid or not params.get('questions'):
                self.codex.answer_questions(request_id,{})
                return
        elif not valid or run.plan_mode or run.options['approvalPolicy'] == 'never':
            self.codex.decide(request_id,'decline')
            return
        run.approvals[request_id] = (method,params)
        self.update_approval_badge(run)

    def update_approval_badge(self, run):
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning) if run.approvals else QIcon()
        run.chat.item.setIcon(0,icon)
        if run.chat is self.active_chat:
            self.refresh_approval()

    def refresh_approval(self):
        run = self.request
        if run is None or not run.approvals:
            self.approval_card.hide()
            return
        method, params = next(iter(run.approvals.values()))
        item = run.turn.items.get(params.get('itemId'),{})
        reason = params.get('reason') or ''
        if method == USER_INPUT_REQUEST:
            title = 'Codex 需要你补充信息'
            target = '\n'.join(question['question'] for question in params['questions'])
            known = True
        elif params.get('networkApprovalContext'):
            network = params['networkApprovalContext']
            title = '允许 Codex 访问网络？'
            target = f"{network.get('protocol','')} · {network.get('host','')}"
            known = bool(network.get('host'))
        elif method == 'item/commandExecution/requestApproval':
            title = '允许 Codex 运行命令？'
            command = params.get('command') or item.get('command') or ''
            cwd = params.get('cwd') or item.get('cwd') or str(run.cwd)
            target = f'{command}\n运行目录：{cwd}'
            known = bool(command)
        else:
            title = '允许 Codex 修改文件？'
            paths = [change['path'] for change in item.get('changes',[]) if change.get('path')]
            root = params.get('grantRoot')
            if root:
                paths.append(f'申请写入目录：{root}')
            target = '\n'.join(paths)
            known = bool(paths)
        decisions = params.get('availableDecisions')
        self.approval_title.setText(title)
        self.approval_detail.setPlainText('\n'.join(part for part in (target,reason) if part))
        self.allow.setText('回答问题' if method == USER_INPUT_REQUEST else '允许本次')
        self.reject.setText('跳过' if method == USER_INPUT_REQUEST else '拒绝')
        self.allow.setEnabled(known and (decisions is None or 'accept' in decisions))
        self.approval_card.show()

    def answer_approval(self, allow):
        run = self.request
        if run is None or not run.approvals or (allow and not self.allow.isEnabled()):
            return
        request_id = next(iter(run.approvals))
        method, params = run.approvals[request_id]
        if method == USER_INPUT_REQUEST:
            answers = self.collect_codex_answers(run, request_id, params['questions']) if allow else {}
            if answers is None or request_id not in run.approvals:
                return
            sent = self.codex.answer_questions(request_id, answers)
        else:
            choices = params.get('availableDecisions') or ()
            rejection = 'cancel' if choices and 'decline' not in choices else 'decline'
            sent = self.codex.decide(request_id, 'accept' if allow else rejection)
        if sent:
            run.approvals.pop(request_id,None)
            self.update_approval_badge(run)

    def collect_codex_answers(self, run, request_id, questions):
        dialog = ProjectDialog(self, '回答 Codex 的问题', '提交')
        fields = []
        for question in questions:
            label = QLabel(question['question'])
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            dialog.body.addWidget(label)
            choices = question.get('options') or []
            if choices:
                field = QComboBox()
                field.addItems([choice['label'] for choice in choices])
                for index, choice in enumerate(choices):
                    field.setItemData(index, choice.get('description',''), Qt.ItemDataRole.ToolTipRole)
                field.setPlaceholderText('选择答案')
                field.setEditable(bool(question.get('isOther')))
                field.setCurrentIndex(-1)
                if field.isEditable():
                    field.lineEdit().setPlaceholderText('选择或输入答案')
                    field.lineEdit().setEchoMode(QLineEdit.EchoMode.Password if question.get('isSecret') else QLineEdit.EchoMode.Normal)
                value = field.currentText
            else:
                field = QLineEdit()
                if question.get('isSecret'):
                    field.setEchoMode(QLineEdit.EchoMode.Password)
                value = field.text
            field.setAccessibleName(question['question'])
            label.setBuddy(field)
            dialog.body.addWidget(field)
            fields.append((question['id'], value))
        run.question_dialog = (request_id, dialog)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        run.question_dialog = None
        answers = {key:{'answers':[answer]} for key, value in fields if (answer := value().strip())}
        dialog.deleteLater()
        return answers if accepted else None

    def interrupt_codex(self, run):
        for request_id, (method, _) in tuple(run.approvals.items()):
            if method != USER_INPUT_REQUEST:
                self.codex.decide(request_id,'cancel')
        if run.question_dialog:
            run.question_dialog[1].reject()
        run.approvals.clear()
        self.update_approval_badge(run)
        self.codex.call('turn/interrupt',{'threadId':run.chat.thread_id,'turnId':run.turn_id},
                        lambda _result,error: self.codex_interrupt_result(run,error))

    def codex_interrupt_result(self, run, error):
        if error and run.chat.request is run:
            run.stopping = False
            run.chat.alert = error
            if run.chat is self.active_chat:
                self.refresh_alert()
                self.refresh_controls()

    def finish_codex(self, run, state, error=''):
        chat = run.chat
        if chat.request is not run:
            return
        chat.request = None
        run.approvals.clear()
        if run.question_dialog:
            run.question_dialog[1].reject()
        self.update_approval_badge(run)
        run.turn.state = state
        if error:
            chat.alert = error[:800]
            chat.failed_turn = run.turn
        if chat is self.active_chat:
            self.refresh_alert()
            self.refresh_controls()
            self.render_history()

    def codex_disconnected(self, error):
        self.catalog.clear()
        self.catalog_state = ''
        for chat in self.chats.values():
            if chat.request is not None:
                self.finish_codex(chat.request,'failed',error)
            if chat.compacting:
                self.finish_compaction(chat,error)

    def cancel_request(self, *, all_chats=False):
        chats = self.chats.values() if all_chats else (self.active_chat,)
        for chat in chats:
            request = chat.request if chat is not None else None
            if request is None or request.stopping:
                continue
            request.stopping = True
            if request.turn_id:
                self.interrupt_codex(request)
            elif not request.turn_sent:
                self.finish_codex(request,'stopped')
        self.stop_playback()
        self.refresh_controls()
        self.render_history()

    def render_history(self, force_bottom=False):
        self.history.render(self.turns,force_bottom)
