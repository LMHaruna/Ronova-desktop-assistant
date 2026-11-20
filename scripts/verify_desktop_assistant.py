"""Verify the native desktop entry and companion phone with isolated services."""

import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QTimer, Qt
from PySide6.QtGui import QCursor, QEnterEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from client_app import ClientWindow
from client_controller import DesktopController
from client_fixtures import FakeCall, fake_router


def wait(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(15)
    assert predicate(), 'Timed out waiting for the native assistant'


def click(widget):
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton)


class FirstPaintProbe(QObject):
    def __init__(self, motion):
        super().__init__()
        self.motion = motion
        self.covered = None

    def eventFilter(self, window, event):
        if self.covered is None and event.type() == QEvent.Type.Paint and window.isVisible():
            self.covered = self.motion.isVisible() and self.motion.frame is not None
        return False


def drag(widget, delta):
    local = QPointF(widget.rect().center())
    origin = QPointF(widget.mapToGlobal(local.toPoint()))
    left = Qt.MouseButton.LeftButton
    none = Qt.MouseButton.NoButton
    for kind, position, button, held in (
        (QEvent.Type.MouseButtonPress, origin, left, left),
        (QEvent.Type.MouseMove, origin+delta, none, left),
        (QEvent.Type.MouseButtonRelease, origin+delta, left, none),
    ):
        QApplication.sendEvent(widget,QMouseEvent(kind,local,position,button,held,Qt.KeyboardModifier.NoModifier))


def assert_smooth(widget, bounds):
    frame = widget.grab().toImage()
    dpr = frame.devicePixelRatio()
    x, y = round(bounds.x()*dpr), round(bounds.y()*dpr)
    width = round(min(52,bounds.width()/2)*dpr)
    assert frame.pixelColor(x,y).alpha() == 0
    assert any(0 < frame.pixelColor(a,b).alpha() < 255
               for a in range(x,x+width) for b in range(y,y+width))


def assert_transparent_phone_corners(frame):
    width, height = frame.width(), frame.height()
    for x in (3, width - 4):
        for y in (3, height - 4):
            assert frame.pixelColor(x, y).alpha() == 0, 'Phone frame filled a transparent corner'


def verify_workspace(phone, output):
    from PySide6.QtGui import QImage
    from client_markup import mark_path

    screen = QApplication.primaryScreen()
    sample = QWidget(None,Qt.WindowType.Tool|Qt.WindowType.FramelessWindowHint|Qt.WindowType.WindowStaysOnTopHint)
    sample.setStyleSheet('background:#21478C')
    sample.resize(220,160)
    sample.move(screen.availableGeometry().center()-QPoint(110,80))
    sample.show(); sample.raise_()
    QTest.qWait(120)
    try:
        phone.start_phone_voice()
        wait(lambda:phone.controller.call_requested and phone.controller.call.active and phone.overlay.phase=='hidden')
        launcher = phone.launcher
        launcher.expand(True)
        icon = launcher.buttons[launcher.keys.index('screenshot')]
        wait(icon.isVisible); click(icon)
        wait(lambda:phone.capture_session is not None and bool(phone.capture_session.overlays))
        assert not launcher.isVisible() and not phone.isVisible()
        assert phone.controller.call_requested and not phone.controller.call.active
        overlay = next(item for item in phone.capture_session.overlays if item.frame.geometry==screen.geometry())
        start = overlay.mapFromGlobal(sample.mapToGlobal(QPoint(32,34)))
        end = overlay.mapFromGlobal(sample.mapToGlobal(QPoint(153,121)))
        def select_region(first, last):
            QCursor.setPos(overlay.mapToGlobal(first))
            QTest.mousePress(overlay,Qt.MouseButton.LeftButton,pos=first)
            QCursor.setPos(overlay.mapToGlobal(last))
            QTest.mouseMove(overlay,last)
            QTest.mouseRelease(overlay,Qt.MouseButton.LeftButton,pos=last)

        select_region(start, end)
        wait(lambda:phone.capture_session.ready)
        actions = overlay.toolbar.findChildren(QPushButton)
        assert {action.objectName() for action in actions} == {'resetCapture','cancelCapture','confirmCapture'}
        assert all(not action.text() and not action.icon().isNull() for action in actions)
        click(overlay.reset_button)
        assert not phone.capture_session.ready
        select_region(start, end)
        QTest.keyClick(overlay,Qt.Key.Key_Space)
        assert len(phone.capture_session.selections) == 1 and not phone.capture_session.ready
        select_region(start + QPoint(12,8), end - QPoint(8,4))
        click(overlay.toolbar.findChild(QPushButton,'confirmCapture'))
        wait(lambda:phone.workspace_window is not None and phone.workspace_window.isVisible() and not phone.panels.motion.isVisible())
        wait(lambda:phone.controller.call.active)
        workspace = phone.workspace
        picture, second_picture = workspace.pictures
        assert second_picture.source.width() < picture.source.width()
        ratio = sample.devicePixelRatioF()
        assert abs(picture.source.width()-(end.x()-start.x())*ratio)<=2
        assert abs(picture.source.height()-(end.y()-start.y())*ratio)<=2
        assert picture.source.pixelColor(picture.source.rect().center()).name()=='#21478c'
        workspace.input.setPlainText('先保留这条问题')
        phone.start_screenshot()
        wait(lambda:phone.capture_session is not None and bool(phone.capture_session.overlays))
        assert not phone.controller.call.active
        overlay = next(item for item in phone.capture_session.overlays if item.frame.geometry==screen.geometry())
        select_region(start, end)
        click(overlay.toolbar.findChild(QPushButton,'cancelCapture'))
        wait(lambda:phone.workspace_window.isVisible() and phone.controller.call.active)
        assert workspace.pictures == (picture, second_picture) and workspace.input.toPlainText()=='先保留这条问题'
        thumbnail = workspace.attachment_row.itemAt(0).widget()
        assert thumbnail.grab().toImage().pixelColor(0,0) != picture.source.pixelColor(0,0)

        click(thumbnail)
        assert workspace.pages.currentWidget() is workspace.chat
        QTest.mouseDClick(thumbnail,Qt.MouseButton.LeftButton)
        assert workspace.pages.currentWidget() is workspace.editor
        editor = workspace.editor
        canvas = editor.canvas
        assert editor.bar.isVisible() and canvas.width() > thumbnail.width()
        assert canvas.grab().toImage().pixelColor(0,0).name() == '#1d2028'
        editor.shape_menu.actions()[0].trigger()
        color_menu = editor.color_button.menu()
        color_menu.popup(editor.color_button.mapToGlobal(QPoint(0,0)))
        blue = editor.color_choices['#2563EB']
        wait(blue.isVisible)
        assert not blue.text()
        click(blue)
        assert canvas.tool == 'line' and canvas.color == '#2563EB'
        first, last = QPoint(canvas.width()//4,canvas.height()//3), QPoint(canvas.width()*3//4,canvas.height()*2//3)
        QTest.mousePress(canvas,Qt.MouseButton.LeftButton,pos=first)
        QTest.mouseMove(canvas,last)
        QTest.mouseRelease(canvas,Qt.MouseButton.LeftButton,pos=last)
        assert len(canvas.marks)==1
        click(editor.undo_button); assert not canvas.marks
        click(editor.redo_button); assert canvas.marks[0].color == '#2563EB'
        click(editor.tools['text'])
        with patch('client_markup.QInputDialog.getText',return_value=('测试标记',True)):
            QTest.mouseClick(canvas,Qt.MouseButton.LeftButton,pos=first)
        assert canvas.marks[-1].text == '测试标记'
        before_drag = canvas.marks[-1]
        click(editor.tools['pointer'])
        center = (mark_path(canvas.image,before_drag).boundingRect().center() * canvas.zoom).toPoint()
        target = center + QPoint(32,24)
        QTest.mousePress(canvas,Qt.MouseButton.LeftButton,pos=center)
        QTest.mouseMove(canvas,target)
        QTest.mouseRelease(canvas,Qt.MouseButton.LeftButton,pos=target)
        assert canvas.marks[-1].points != before_drag.points
        click(editor.undo_button); assert canvas.marks[-1] == before_drag
        click(editor.undo_button); assert len(canvas.marks) == 1
        assert phone.workspace_window.grab().save(str(output/'workspace-markup.png'))
        grip = next(button for button in editor.bar.findChildren(QPushButton)
                    if button.accessibleName() == '拖动标记工具栏')
        bar_position = editor.bar.pos()
        drag(grip, QPointF(20,-25))
        assert editor.bar.pos() == bar_position + QPoint(20,-25)
        click(workspace.editor.findChild(QPushButton,'markupDone'))
        marked, other = workspace.pictures
        assert marked.source==picture.source and marked.preview!=picture.preview and other is second_picture
        QTest.mouseDClick(workspace.attachment_row.itemAt(0).widget(),Qt.MouseButton.LeftButton)
        click(editor.tools['erase'])
        start_point, end_point = marked.marks[0].points
        midpoint = QPoint(*(round((a+b)*canvas.zoom/2) for a,b in zip(start_point,end_point)))
        QTest.mouseClick(canvas,Qt.MouseButton.LeftButton,pos=midpoint)
        assert not canvas.marks
        QTest.keyClick(workspace.editor.canvas,Qt.Key.Key_Escape)
        assert workspace.pages.currentWidget() is workspace.chat
        assert workspace.pictures == (marked, second_picture) and len(marked.marks)==1
        assert workspace.send.isVisible() and workspace.send.isEnabled()

        codex = phone.codex
        codex.default_cwd.mkdir(parents=True,exist_ok=True)
        codex.media.mkdir(parents=True,exist_ok=True)
        sent = []
        def rpc(method, params, callback):
            if method == 'thread/start':
                callback({'thread':{'id':'native-fixture'}},'')
                return
            assert method == 'turn/start'
            sent.append(params)
            if len(sent) == 1:
                callback({},'离线模拟 Codex 请求失败')
                return
            turn = {'id':f'fixture-turn-{len(sent)}'}
            callback({'turn':turn},'')
            turn.update(status='completed',items=[
                {'type':'commandExecution','id':'command','command':'Get-Location','status':'completed','exitCode':0},
                {'type':'agentMessage','id':'reply','text':'看到了标记的位置。'}])
            codex.event.emit('turn/completed',{'threadId':params['threadId'],'turn':turn})

        with (patch.object(codex,'account',{'type':'chatgpt'}),
              patch.object(codex,'call',side_effect=rpc)):
            workspace.refresh_controls()
            click(workspace.send)
            wait(workspace.alert.isVisible)
            assert '离线模拟 Codex 请求失败' in workspace.alert_text.text()
            click(workspace.retry)
            wait(lambda:workspace.request is None and workspace.turns[-1].state=='done')
            workspace.input.setPlainText('继续看这两张图')
            click(workspace.send)
            wait(lambda:workspace.request is None and workspace.turns[-1].state=='done')
            assert {item['threadId'] for item in sent} == {'native-fixture'}
            paths = {tuple(image['path'] for image in item['input'][1:]) for item in sent}
            assert len(paths) == 1
            images = next(iter(paths))
            assert len(images) == 2
            for path, expected in zip(images, (marked, second_picture)):
                assert QImage(path).convertToFormat(expected.preview.format()) == expected.preview
            assert sent[0]['input'][0]['text'] == '先保留这条问题'
            assert sent[-1]['input'][0]['text'] == '继续看这两张图'
            assert phone.controller.records()[-1]['user']=='触发测试错误'
        wait(lambda:len(workspace.history.rows)==2 and all(row.answer.isVisible() for row in workspace.history.rows.values()))
        QTest.qWait(60)
        assert 'Get-Location' not in workspace.history.toPlainText()
        for row in workspace.history.rows.values():
            images = row.findChildren(QPushButton,'workHistoryImage')
            assert len(images) == 2 and all(row.bubble.rect().contains(image.geometry()) for image in images)
        assert phone.workspace_window.grab().save(str(output/'workspace-answered.png'))
        workspace.input.setPlainText('保留这条草稿')
        remove = workspace.attachment_row.itemAt(0).widget().findChild(QPushButton,'workRemoveImage')
        assert remove.isVisible()
        click(remove)
        assert workspace.pictures == (second_picture,) and workspace.attachment.isVisible()
        click(workspace.attachment_row.itemAt(0).widget().findChild(QPushButton,'workRemoveImage'))
        assert not workspace.pictures and not workspace.attachment.isVisible()
        assert workspace.input.toPlainText() == '保留这条草稿' and len(workspace.history.rows) == 2
        phone.open_companion()
        wait(lambda:phone.isVisible() and phone.controller.call.active and not phone.panels.motion.isVisible())
        phone.end_or_decline()
        wait(lambda:not phone.controller.call_requested and phone.overlay.phase=='shown')
        return {'screenshot_scale':ratio,'captured_pixels':[picture.source.width(),picture.source.height()]}
    finally:
        sample.close()


def main():
    output = ROOT / 'runtime/verification-records/desktop-assistant'
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    original_cursor = QCursor.pos()
    with tempfile.TemporaryDirectory(dir=output) as profile, \
            patch('requests.sessions.Session.request', side_effect=AssertionError('No network')), \
            patch('voice_input.LiveMicrophone.start', side_effect=AssertionError('No microphone')), \
            patch('client_codex.CodexAppServer.start'), \
            patch('client_models.environment', return_value='fixture-key'):
        router, chat, voice = fake_router()
        ringtone = Mock()
        ringtone.stop.return_value = True
        controller = DesktopController(profile, router=router, call_factory=FakeCall,
                                       ringtone_factory=lambda *a, **kw: ringtone)
        phone = ClientWindow(controller, ROOT, desktop=True)
        launcher = phone.launcher
        motion = phone.panels.motion

        def wait_open(window):
            wait(lambda: window.isVisible() and not motion.isVisible())

        try:
            QCursor.setPos(launcher.mapToGlobal(launcher.center) - QPoint(220, 130))
            phone.restore_window()
            QTest.qWait(70)
            assert launcher.isVisible() and not phone.isVisible() and not phone.settings_window.isVisible()
            assert not any(button.isVisible() for button in launcher.buttons)
            assert_smooth(launcher, launcher.avatar.geometry())
            assert not controller.call.starts and 'live2d' not in sys.modules
            assert launcher.grab().save(str(output/'desktop-avatar.png'))
            pointer = launcher.avatar.mapToGlobal(launcher.avatar.rect().center())
            with patch('client_shell.QCursor') as cursor:
                cursor.pos.return_value = pointer
                center = QPointF(launcher.avatar.rect().center())
                app.sendEvent(launcher.avatar, QEnterEvent(center, QPointF(launcher.center), QPointF(pointer)))
                wait(lambda: all(button.isVisible() for button in launcher.buttons))
                QTest.qWait(600)
            assert launcher.grab().save(str(output/'desktop-orbit.png'))
            for icon in launcher.buttons:
                assert_smooth(launcher, icon.geometry())
            paint_probe = FirstPaintProbe(motion)
            phone.installEventFilter(paint_probe)
            click(launcher.buttons[0])
            assert motion.isVisible() and motion.opening and motion.source == launcher.icon_rect('companion')
            wait_open(phone)
            phone.removeEventFilter(paint_probe)
            assert paint_probe.covered is True, 'The opening snapshot disappeared before the window painted'
            assert phone.overlay.phase == 'shown'
            pane = phone.text_pane
            assert phone.width() <= 374 and not pane.call_button.isVisible()
            assert_transparent_phone_corners(phone.grab(phone.overlay.navigation_bounds()).toImage())
            assert phone.grab().save(str(output/'companion-chat.png'))
            start = phone.pos()
            drag(phone.phone_grip, QPointF(-24 if start.x() >= 24 else 24, 0))
            assert phone.pos() != start
            phone_position = phone.pos()
            pane.input.setPlainText('测试发送')
            click(pane.send)
            wait(lambda: bool(controller.records()) and controller.records()[-1]['state'] == 'completed')
            assert chat.calls == 1 and controller.records()[-1]['user'] == '测试发送'
            wait(lambda: '测试发送' in pane.history.toPlainText())
            chat.fail = True
            pane.input.setPlainText('触发测试错误')
            click(pane.send)
            wait(phone.reply_alert.isVisible)
            assert '离线模拟连接失败' in phone.reply_alert.text()
            assert phone.reply_alert.geometry().top() < phone.height()/4
            assert phone.grab().save(str(output/'companion-alert.png'))
            chat.fail = False
            phone.reply_alert.hide()

            pane.input.setPlainText('保留这条草稿')
            options = pane.call_options
            assert options.plus.size() == pane.send.size()
            assert pane.send.y() == options.plus.y()
            assert pane.send.x() - options.plus.geometry().right() - 1 == 10
            assert pane.input.height() >= 84
            assert not hasattr(phone.settings_pane,'oath_row')
            click(options.plus)
            assert options.isVisible() and options.video.isEnabled() and not controller.call.starts
            assert pane.rect().contains(options.geometry()) and options.oath.isVisible()
            assert not options.oath.isEnabled() and '达到100' in options.oath.toolTip()
            click(options.oath)
            assert phone.oath_dialog is None and options.isVisible()
            assert phone.grab().save(str(output/'companion-call-menu.png'))
            unlocked = {**controller.memory.profile(), 'affinity':100}
            with (patch.object(controller.memory,'profile',return_value=unlocked),
                  patch.object(phone,'_oath_unlock_notified',True),
                  patch('client_oath_panel.OathDialog.auto_start')):
                phone.sync_oath_entry()
                assert options.oath.isEnabled() and not options.oath.text()
                click(options.oath)
                dialog = phone.oath_dialog
                try:
                    assert dialog is not None and dialog.isVisible() and dialog.parentWidget() is phone
                    assert not options.isVisible()
                finally:
                    if dialog is not None: dialog.reject()
            click(options.plus)
            assert options.isVisible() and not options.oath.isEnabled()
            QTest.keyClick(options.plus, Qt.Key.Key_Escape)
            assert not options.isVisible()
            click(options.plus); click(options.voice)
            wait(lambda: phone.overlay.phase == 'hidden' and controller.call_requested)
            assert len(controller.call.starts) == 1
            assert phone.grab().save(str(output/'companion-voice.png'))
            click(phone.text_button)
            wait(lambda: controller.snapshot()['speaker_muted'])
            assert controller.playback_volume() == 0 and controller.preferences.volume > 0
            wait(lambda: not phone.state['busy'] and phone.voice_button.isEnabled())
            click(phone.voice_button)
            wait(lambda: controller.muted and not controller.call.active)
            click(phone.end_button)
            transition = phone.overlay.transition
            assert transition.snapshot is not None
            transition.animation.pause()
            try:
                for progress in (0, .25, .5, .75, 1):
                    transition.set_progress(progress)
                    frame = phone.grab(transition.geometry()).toImage()
                    assert_transparent_phone_corners(frame)
                    assert frame.pixelColor(frame.width() // 2, frame.height() // 2).alpha() >= 253
            finally:
                transition.animation.resume()
            wait(lambda: phone.overlay.phase == 'shown' and not controller.call_requested)
            assert pane.input.toPlainText() == '保留这条草稿'
            click(phone.phone_dismiss)
            assert not phone.isVisible() and launcher.isVisible()
            phone.open_companion()
            wait_open(phone)
            assert phone.isVisible() and pane.input.toPlainText() == '保留这条草稿'
            assert phone.pos() == phone_position

            phone.settings()
            assert motion.isVisible() and not motion.opening
            assert motion.destination == launcher.icon_rect('companion', moving=True)
            wait_open(phone.settings_window)
            assert not phone.isVisible()
            assert_smooth(phone.settings_window, phone.settings_window.rect())
            assert phone.settings_window.grab().save(str(output/'assistant-settings.png'))
            phone.settings_pane.desktop_pin.click()
            wait(lambda: not controller.preferences.desktop_pinned and
                 not launcher.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
            phone.open_workspace()
            assert motion.isVisible() and not motion.opening
            wait_open(phone.workspace_window)
            assert not phone.settings_window.isVisible()
            assert_smooth(phone.workspace_window, phone.workspace_window.rect())
            window,workspace = phone.workspace_window,phone.workspace
            radius = workspace.INNER_CORNER_RADIUS
            composer = workspace.findChild(QWidget,'workComposer')
            assert workspace.sidebar.mapTo(window,QPoint(radius,workspace.sidebar.height()-radius)) == (
                QPoint(window.corner_radius,window.height()-window.corner_radius))
            assert composer.mapTo(window,QPoint(composer.width()-radius,composer.height()-radius)) == (
                QPoint(window.width()-window.corner_radius,window.height()-window.corner_radius))
            assert phone.text_pane.send.size() == workspace.send.size()
            assert phone.text_pane.send.styleSheet() == workspace.send.styleSheet()
            assert phone.workspace_window.grab().save(str(output/'assistant-workspace.png'))
            assert phone.workspace.model_button.text() == 'Codex ▼'
            click(phone.workspace.findChild(QPushButton,'workCodexLogin'))
            wait_open(phone.settings_window)
            wait(phone.settings_pane.codex_login_button.isVisible)
            assert phone.settings_pane.stack.currentWidget() is phone.settings_pane.model_page
            phone.open_companion()
            wait_open(phone)
            assert not phone.workspace_window.isVisible()

            with patch('client_shell.QCursor') as cursor:
                cursor.pos.return_value = launcher.avatar.mapToGlobal(launcher.avatar.rect().center())
                launcher.expand(True)
                wait(lambda: all(button.isVisible() for button in launcher.buttons))
                for key, window in (('companion',phone),('workspace',phone.workspace_window),('settings',phone.settings_window)):
                    icon = launcher.buttons[launcher.keys.index(key)]
                    if not window.isVisible():
                        click(icon)
                        wait_open(window)
                    click(icon)
                    assert motion.isVisible() and not motion.opening
                    assert motion.destination == launcher.icon_rect(key, moving=True)
                    wait(lambda: not motion.isVisible() and not window.isVisible())
                click(launcher.buttons[0])
                QTest.qWait(100)
                assert motion.isVisible() and motion.opening
                click(launcher.buttons[0])
                assert motion.animation.direction().name == 'Backward'
                wait(lambda: not motion.isVisible() and not phone.isVisible())
                click(launcher.buttons[0])
                wait_open(phone)

            click(options.plus); click(options.voice)
            wait(lambda: phone.overlay.phase == 'hidden' and controller.call_requested)
            click(phone.phone_dismiss)
            wait(lambda: not controller.call_requested)
            assert not phone.isVisible() and launcher.isVisible()

            controller.test_incoming_call().result(4)
            wait(lambda: phone.isVisible() and phone.screen == 'call' and phone.state['incoming'])
            assert len(controller.call.starts) == 2
            click(phone.end_button)
            wait(lambda: not controller.snapshot()['incoming'] and phone.overlay.phase == 'shown')
            if '--video' in sys.argv:
                click(options.plus); click(options.video)
                wait(lambda: phone.video_call and controller.call_requested)
                assert not phone.assets.video_portrait.isNull()
                assert len(controller.call.starts) == 3
                assert phone.grab().save(str(output/'companion-video.png'))
                assert_transparent_phone_corners(phone.grab(phone.overlay.navigation_bounds()).toImage())
                click(phone.text_button); wait(lambda:controller.snapshot()['speaker_muted'])
                click(phone.end_button)
                wait(lambda: phone.overlay.phase == 'shown' and not controller.call_requested)
                assert not phone.video_call
                assert pane.input.toPlainText() == '保留这条草稿'
                click(options.plus); click(options.video)
                wait(lambda:phone.video_call and controller.call_requested, 6)
                assert len(controller.call.starts) == 4
                click(phone.end_button)
                wait(lambda:phone.overlay.phase == 'shown' and not controller.call_requested)
            workspace_metrics = verify_workspace(phone,output) if '--workspace' in sys.argv else {}
            phone.quit_client()
            wait(lambda: controller.closed and not launcher.isVisible() and not phone.video_call)
            report = {'passed': True, 'native_windows': True, 'icon_toggles': True, 'chat_and_call_fixtures': True,
                      'live2d_loaded_in_client': 'live2d' in sys.modules, 'video_call': '--video' in sys.argv,
                      'microphone_started': False, 'cloud_requests': 0, **workspace_metrics}
            (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(report))
        finally:
            if not controller.closed:
                controller.close().result(8)
            launcher.close()
            phone.settings_window.close()
            phone.close()
            QCursor.setPos(original_cursor)


if __name__ == '__main__':
    main()
