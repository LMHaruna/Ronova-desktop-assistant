"""Workspace transcript with native message bubbles and folded execution details."""
from math import ceil
from pathlib import PureWindowsPath

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QDesktopServices, QFont, QFontMetrics, QTextBlockFormat, QTextCharFormat,
                           QTextCursor, QTextDocument, QTextOption)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLayout, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from client_fonts import UI_FONT_SIZE, code_font, scaled_size, typography
from client_workspace_controls import PicturePreview


class MessageText(QTextBrowser):
    def __init__(self):
        super().__init__()
        self.setObjectName('workMessageText')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self.open_link)
        font = QFont()
        font.setPixelSize(scaled_size(UI_FONT_SIZE))
        self.document().setDefaultFont(font)
        self.document().setDocumentMargin(0)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.source = None
        self.markdown = False
        typography().changed.connect(self.update_typography)

    def update_typography(self, scale):
        font = self.document().defaultFont()
        font.setPixelSize(scaled_size(UI_FONT_SIZE))
        self.document().setDefaultFont(font)
        if self.source is not None:
            text, markdown = self.source, self.markdown
            self.source = None
            self.show_text(text, markdown=markdown)
        else:
            self.fit_height()

    def loadResource(self, kind, url):
        # Replies can contain untrusted Markdown. Rendering must not read
        # arbitrary local files or fetch images from a remote server.
        return None

    @staticmethod
    def open_link(url):
        if url.scheme() in ('https','http') and url.host():
            QDesktopServices.openUrl(url)

    def show_text(self, text, *, markdown=False):
        if text == self.source:
            return
        self.source = text
        self.markdown = markdown
        document = self.document()
        if markdown:
            flags = QTextDocument.MarkdownFeature
            document.setMarkdown(text,flags.MarkdownDialectGitHub | flags.MarkdownNoHTML)
        else:
            document.setPlainText(text)
        cursor = QTextCursor(document)
        block = document.begin()
        cursor.beginEditBlock()
        if markdown:
            font = QTextCharFormat()
            font.setFontFamilies(document.defaultFont().families())
            font.setFontFixedPitch(False)
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.mergeCharFormat(font)
        while block.isValid():
            cursor.setPosition(block.position())
            fmt = block.blockFormat()
            fmt.setLineHeight(123,QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            if markdown:
                if not fmt.nonBreakableLines():
                    fmt.setTopMargin(0)
                    fmt.setBottomMargin(3 if block.textList() else 7)
                fmt.setNonBreakableLines(False)
            cursor.setBlockFormat(fmt)
            block = block.next()
        cursor.endEditBlock()
        self.fit_height()

    def fit_height(self):
        document = self.document()
        width = max(1,self.viewport().width())
        if document.textWidth() != width:
            document.setTextWidth(width)
        height = ceil(document.size().height()) + 2
        if self.height() != height:
            self.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_height()

    def wheelEvent(self, event):
        event.ignore()

    def sizeHint(self):
        return QSize(0,self.height())

    def minimumSizeHint(self):
        return self.sizeHint()


class TranscriptFoldout(QWidget):
    def __init__(self, accessible_name):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(9)
        self.button = QToolButton()
        self.button.setObjectName('workTrace')
        self.button.setCheckable(True)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.toggled.connect(self.toggle)
        layout.addWidget(self.button,0,Qt.AlignmentFlag.AlignLeft)
        self.detail = QPlainTextEdit(readOnly=True)
        self.detail.setObjectName('workTraceDetail')
        self.detail.setFont(code_font())
        self.detail.setProperty('_ronova_code_font', True)
        self.detail.setAccessibleName(accessible_name)
        self.detail.setFixedHeight(170)
        layout.addWidget(self.detail)
        self.content = ''
        self.toggle(False)

    def set_content(self, title, content):
        self.content = content
        self.button.setText(title)
        self.setVisible(bool(content))
        self.toggle(self.button.isChecked())

    def toggle(self, expanded):
        self.button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.detail.setVisible(expanded and bool(self.content))
        if expanded and self.detail.toPlainText() != self.content:
            scroll = self.detail.verticalScrollBar()
            position = scroll.value()
            self.detail.setPlainText(self.content)
            scroll.setValue(position)


class TurnView(QWidget):
    def __init__(self, turn, select_picture, reply_icons, play_reply):
        super().__init__()
        self.turn = turn
        self.last_state = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(9)
        row = QHBoxLayout()
        row.addStretch(1)
        self.bubble = QFrame()
        self.bubble.setObjectName('workUserBubble')
        user = QVBoxLayout(self.bubble)
        user.setContentsMargins(14,9,14,9)
        user.setSpacing(6)
        self.question = MessageText()
        self.question.setAccessibleName('你的消息')
        self.question.show_text(turn.question)
        user.addWidget(self.question)
        self.picture_width = 0
        for picture in turn.pictures:
            image = PicturePreview()
            image.setObjectName('workHistoryImage')
            image.set_image(picture.preview)
            image.setAccessibleName('查看或标记这条消息的截图')
            image.clicked.connect(lambda checked=False, selected=picture: select_picture(selected))
            user.addWidget(image,0,Qt.AlignmentFlag.AlignLeft)
            self.picture_width = max(self.picture_width, image.width())
        row.addWidget(self.bubble)
        layout.addLayout(row)
        layout.addSpacing(3)

        self.answer = MessageText()
        self.answer.setAccessibleName('Codex 回复')
        answer_layout = QVBoxLayout()
        answer_layout.setSpacing(1)
        answer_layout.addWidget(self.answer)
        layout.addLayout(answer_layout)
        self.reply_icons = reply_icons
        self.speaker = None
        if reply_icons is not None:
            self.speaker = QPushButton()
            self.speaker.setObjectName('workSpeaker')
            self.speaker.setIconSize(QSize(20,20))
            self.speaker.setFixedSize(28,28)
            self.speaker.setCursor(Qt.CursorShape.PointingHandCursor)
            self.speaker.clicked.connect(lambda: play_reply(turn, self.answer.toPlainText()))
            self.set_playing(False)
            answer_layout.addWidget(self.speaker,0,Qt.AlignmentFlag.AlignLeft)
        self.status = QLabel()
        self.status.setObjectName('workHint')
        layout.addWidget(self.status)
        self.changed_files = TranscriptFoldout('本轮修改的文件')
        self.changed_files.detail.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.changed_files)
        self.execution = TranscriptFoldout('命令输出与文件修改状态')
        layout.addWidget(self.execution)

    def set_playing(self, playing):
        if self.speaker is not None:
            label = '停止播放这条回复' if playing else '播放这条回复'
            self.speaker.setIcon(self.reply_icons[playing])
            self.speaker.setToolTip(label)
            self.speaker.setAccessibleName(label)

    def fit_bubble(self, available):
        metrics = QFontMetrics(self.question.document().defaultFont())
        content = max((metrics.horizontalAdvance(line) for line in self.turn.question.splitlines()),default=0)
        content = max(content, self.picture_width)
        self.bubble.setFixedWidth(max(44,min(content + 32,round(available * .78),620)))

    def refresh(self):
        self.last_state = self.turn.state
        replies, notes, logs = [], [], []
        files = {}
        commands = other_patches = 0
        for item in self.turn.items.values():
            kind = item['type']
            if kind == 'agentMessage' and item.get('text'):
                messages = notes if item.get('phase') == 'commentary' else replies
                messages.append(item['text'])
            elif kind == 'plan' and item.get('text'):
                replies.append('**计划**\n\n' + item['text'])
            elif kind == 'commandExecution':
                commands += 1
                logs.append(self.command_log(item))
            elif kind == 'fileChange':
                changes = item.get('changes',[])
                if item.get('status') == 'completed':
                    for change in changes:
                        if path := change.get('path'):
                            destination = (change.get('kind') or {}).get('move_path')
                            if destination:
                                files.pop(PureWindowsPath(path),None)
                                files[PureWindowsPath(destination)] = f'{path} → {destination}'
                            else:
                                files.setdefault(PureWindowsPath(path),path)
                else:
                    other_patches += 1
                    paths = '\n'.join(change['path'] for change in changes if change.get('path'))
                    logs.append('\n'.join(part for part in ('文件修改 ' + self.item_status(item),paths) if part))
        folded_notes = bool(replies and notes)
        if folded_notes:
            logs.insert(0,'处理过程\n' + '\n\n'.join(notes))
        visible_replies = replies or notes
        self.answer.show_text('\n\n'.join(visible_replies),markdown=True)
        self.answer.setVisible(bool(visible_replies))
        if self.speaker is not None:
            self.speaker.setVisible(self.turn.state == 'done' and bool(replies))
        status = {'pending':'正在处理…','failed':'回复失败','stopped':'已停止','done':''}[self.turn.state]
        self.status.setText(status)
        self.status.setVisible(bool(status) and (self.turn.state != 'pending' or not visible_replies))
        self.changed_files.detail.setFixedHeight(min(170,30 + self.changed_files.detail.fontMetrics().lineSpacing()*len(files)))
        self.changed_files.set_content(f'修改的文件（{len(files)}）','\n'.join(files.values()))
        labels = []
        if commands:
            labels.append(f'{commands} 条命令')
        if other_patches:
            labels.append(f'{other_patches} 条文件修改记录')
        if folded_notes:
            labels.append('处理过程')
        self.execution.set_content('执行详情 · ' + ' · '.join(labels),'\n\n'.join(logs))

    @staticmethod
    def item_status(item):
        return {'inProgress':'进行中','completed':'完成','failed':'失败','declined':'已拒绝',
                'cancelled':'已取消'}.get(item.get('status'),'')

    @classmethod
    def command_log(cls, item):
        lines = ['命令 ' + cls.item_status(item),item.get('command') or '',item.get('cwd') or '']
        if isinstance(item.get('exitCode'),int):
            lines.append(f"退出码 {item['exitCode']}")
        lines.append((item.get('aggregatedOutput') or '')[-16000:])
        return '\n'.join(line for line in lines if line)


class WorkspaceHistory(QScrollArea):
    play_requested = Signal(object, str)

    def __init__(self, select_picture, reply_icons=None):
        super().__init__()
        self.setObjectName('workHistory')
        self.setAccessibleName('工作区对话')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidgetResizable(True)
        self.body = QWidget()
        self.body.setObjectName('workHistoryBody')
        self.column = QVBoxLayout(self.body)
        self.column.setContentsMargins(10,14,10,13)
        self.column.setSpacing(25)
        self.column.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.column.addStretch(1)
        self.setWidget(self.body)
        self.select_picture = select_picture
        self.reply_icons = reply_icons
        self.rows = {}
        self.scroll_target = None
        self.scroll_timer = QTimer(self,singleShot=True,interval=0)
        self.scroll_timer.timeout.connect(self.apply_scroll)
        self.viewport().installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.viewport() and event.type() == QEvent.Type.Resize:
            for row in self.rows.values():
                row.fit_bubble(self.viewport().width() - 20)
        return super().eventFilter(watched,event)

    def render(self, turns, force_bottom=False):
        bar = self.verticalScrollBar()
        self.scroll_target = None if force_bottom or bar.value() >= bar.maximum() - 6 else bar.value()
        active = set(turns)
        for turn in tuple(self.rows):
            if turn not in active:
                row = self.rows.pop(turn)
                self.column.removeWidget(row)
                row.hide()
                row.deleteLater()
        for index,turn in enumerate(turns):
            new = turn not in self.rows
            if new:
                self.rows[turn] = TurnView(turn,self.select_picture,self.reply_icons,self.play_requested.emit)
            row = self.rows[turn]
            if self.column.indexOf(row) != index:
                self.column.removeWidget(row)
                self.column.insertWidget(index,row)
            if new:
                row.fit_bubble(self.viewport().width() - 20)
                row.show()
            if row.last_state != turn.state or turn.state == 'pending':
                row.refresh()
        self.scroll_timer.start()

    def apply_scroll(self):
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum() if self.scroll_target is None else self.scroll_target)

    def toPlainText(self):
        """Return the transcript text displayed by this view."""
        text = []
        for index in range(self.column.count()-1):
            row = self.column.itemAt(index).widget()
            text.extend((row.question.toPlainText(),row.answer.toPlainText()))
            for foldout in (row.changed_files,row.execution):
                if foldout.button.isChecked() and foldout.content:
                    text.append(foldout.content)
        return '\n'.join(text)
