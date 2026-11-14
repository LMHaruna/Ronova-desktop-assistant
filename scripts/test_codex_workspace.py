"""Exercise the workspace against a local JSONL App Server fixture."""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QDragEnterEvent, QDragLeaveEvent, QDropEvent, QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLineEdit, QMenu, QMessageBox, QPushButton

from client_codex import CodexAppServer
from client_markup import Mark
from client_tool_library import discover_library
from client_workspace import Picture, WorkspacePane, WorkspaceTurn


SERVER = '''
import json, os, sys
from pathlib import Path

history = Path(os.environ['CODEX_HOME']) / 'fixture.json'
archived_history = history.with_name('fixture-archived.json')
signed_in = history.exists()
thread = {}
turns = []
counter = 0
mode = 'default'

def emit(**message):
    print(json.dumps(message), flush=True)

def event(method, **params):
    emit(method=method, params=params)

for line in sys.stdin:
    msg = json.loads(line)
    method, request = msg.get('method'), msg.get('id')
    params = msg.get('params', {})
    if request is None:
        continue
    result = {}
    if method == 'initialize':
        assert params['capabilities']['experimentalApi'] is True
    elif method == 'account/read':
        result = {'account': {'type':'chatgpt'} if signed_in else None}
    elif method == 'account/login/start':
        assert params == {'type':'chatgpt'}
        emit(id=request,result={'loginId':'test-login','authUrl':'https://auth.openai.com/fixture'})
        signed_in = True
        event('account/login/completed',loginId='test-login',success=True)
        continue
    elif method == 'account/rateLimits/read':
        assert signed_in
        result = {'rateLimits': {'limitId':'codex_other',
                                'primary':{'usedPercent':90,'windowDurationMins':300}},
                  'rateLimitsByLimitId': {'codex': {'limitId':'codex',
                      'primary':{'usedPercent':28,'windowDurationMins':300},
                      'secondary':{'usedPercent':40,'windowDurationMins':10080}}}}
    elif method == 'model/list':
        result = {'data':[
            {'model':'fixture-default','displayName':'Fixture Default','isDefault':True,
             'defaultReasoningEffort':'medium','supportedReasoningEfforts':[{'reasoningEffort':'medium'}]},
            {'model':'fixture-vision','displayName':'Fixture Vision','inputModalities':['text','image'],
             'defaultReasoningEffort':'high','supportedReasoningEfforts':[
                 {'reasoningEffort':'high'},{'reasoningEffort':'xhigh'}]}]}
    elif method == 'thread/list':
        data = []
        if params.get('archived'):
            if archived_history.exists():
                data.append(json.loads(archived_history.read_text(encoding='utf-8'))[0])
        elif history.exists():
            thread, turns = json.loads(history.read_text(encoding='utf-8'))
            data.append(thread)
        result = {'data':[record for record in data if not params.get('cwd') or params['cwd'] == record['cwd']]}
    elif method == 'thread/turns/list':
        assert params['itemsView'] == 'full'
        stored_turns = turns
        if archived_history.exists():
            archived_thread, archived_turns = json.loads(archived_history.read_text(encoding='utf-8'))
            if params['threadId'] == archived_thread['id']:
                stored_turns = archived_turns
        result = {'data':list(reversed(stored_turns))}
    elif method in ('thread/start','thread/resume'):
        assert signed_in and params['approvalPolicy'] == 'on-request'
        assert not {'baseInstructions', 'developerInstructions', 'personality'} & params.keys()
        if method == 'thread/start':
            thread = {'id':'thread-fixture','cwd':params['cwd'],'preview':'检查截图',
                      'model':params.get('model'),'approvalsReviewer':params['approvalsReviewer']}
        result = {'thread':thread}
    elif method == 'turn/start':
        assert params['cwd'] == thread['cwd']
        assert not {'baseInstructions', 'developerInstructions', 'personality'} & params.keys()
        assert params['sandboxPolicy']['networkAccess'] is False
        assert params['model'] == 'fixture-vision' and params['effort'] == 'xhigh'
        collaboration = params.get('collaborationMode')
        if collaboration:
            assert collaboration['settings'] == {'model':'fixture-vision','reasoning_effort':'xhigh',
                                                 'developer_instructions':None}
            mode = collaboration['mode']
        if params['input'][0]['text'] == '制定修改计划':
            assert mode == 'plan'
            assert params['approvalPolicy'] == 'never' and params['approvalsReviewer'] == 'user'
            assert params['sandboxPolicy']['type'] == 'readOnly'
            turn = {'id':'planning','status':'inProgress','items':[]}
            keys = {'threadId':thread['id'],'turnId':turn['id']}
            emit(id=request,result={'turn':turn})
            emit(id='questions',method='item/tool/requestUserInput',params={**keys,'itemId':'questions',
                 'questions':[{'id':'scope','header':'范围','question':'要处理哪个部分？',
                               'options':[{'label':'界面','description':'只处理界面'}]}]})
            continue
        assert mode == 'default'
        if params['approvalPolicy'] == 'never':
            assert params['sandboxPolicy']['type'] == 'readOnly'
            assert params['approvalsReviewer'] == 'user'
            turn = {'id':'read-only','status':'inProgress','items':[]}
            emit(id=request,result={'turn':turn})
            turn.update(status='completed',items=[{'type':'agentMessage','id':'read','text':'只读模式完成'}])
            event('turn/completed',threadId=thread['id'],turn=turn)
            continue
        assert params['sandboxPolicy']['writableRoots'] == [thread['cwd']]
        thread['reasoningEffort'] = params['effort']
        counter += 1
        turn = {'id':f'turn-{counter}','status':'inProgress',
                'items':[{'type':'userMessage','id':'input','content':params['input']}]}
        keys = {'threadId':thread['id'],'turnId':turn['id']}
        item = {'type':'commandExecution','id':'command','command':'Get-Location',
                'cwd':thread['cwd'],'status':'inProgress'}
        turn['items'].append(item)
        event('turn/started',threadId=thread['id'],turn=turn)
        emit(id=request,result={'turn':turn})
        event('item/started',**keys,item=item)
        emit(id='approval',method='item/commandExecution/requestApproval',
             params={**keys,'itemId':'command','command':item['command'],'cwd':item['cwd']})
        continue
    elif request == 'questions':
        assert msg['result'] == {'answers':{'scope':{'answers':['界面']}}}
        event('serverRequest/resolved',threadId=thread['id'],requestId=request)
        event('item/plan/delta',**keys,itemId='plan',delta='第一步')
        turn['items'] = [{'type':'plan','id':'plan','text':'第一步：检查界面'}]
        turn['status'] = 'completed'
        event('turn/completed',threadId=thread['id'],turn=turn)
        continue
    elif request == 'approval':
        decision = msg['result']['decision']
        if decision == 'cancel':
            continue
        assert decision in ('accept','decline')
        item['status'] = 'completed' if decision == 'accept' else 'declined'
        if decision == 'accept':
            event('item/commandExecution/outputDelta',**keys,itemId='command',delta='cwd result')
            item['exitCode'] = 0
            item['aggregatedOutput'] = 'cwd result'
        event('item/completed',**keys,item=item)
        if decision == 'accept':
            patches = (
                ('patch-one','completed',('src/main.py','docs/说明.md')),
                ('patch-two','completed',('src/main.py',)),
                ('patch-rejected','declined',('rejected.txt',)),
            )
            for patch_id, status, paths in patches:
                change = {'type':'fileChange','id':patch_id,'status':status,
                          'changes':[{'path':path,'kind':{'type':'update'},'diff':''} for path in paths]}
                turn['items'].append(change)
                event('item/completed',**keys,item=change)
        answer = '处理完成' if decision == 'accept' else '操作已拒绝'
        event('item/agentMessage/delta',**keys,itemId='reply',delta=answer)
        turn['items'].append({'type':'agentMessage','id':'reply','text':answer})
        turn['status'] = 'completed'
        turns.append(turn)
        history.write_text(json.dumps([thread,turns]),encoding='utf-8')
        event('thread/tokenUsage/updated',**keys,tokenUsage={
            'last':{'totalTokens':1200},'total':{'totalTokens':44000},'modelContextWindow':8000})
        event('turn/completed',threadId=thread['id'],turn=turn)
        continue
    elif method == 'thread/compact/start':
        assert params == {'threadId':thread['id']}
        emit(id=request,result={})
        event('thread/tokenUsage/updated',threadId=thread['id'],turnId='compact',
              tokenUsage={'last':{'totalTokens':200},'modelContextWindow':8000})
        event('turn/completed',threadId=thread['id'],turn={'id':'compact','status':'completed','items':[]})
        continue
    elif method == 'thread/name/set':
        assert params['threadId'] == thread['id']
        thread['name'] = params['name']
        if history.exists():
            history.write_text(json.dumps([thread,turns]),encoding='utf-8')
        event('thread/name/updated',threadId=thread['id'],threadName=thread['name'])
    elif method == 'thread/delete':
        assert params == {'threadId':thread['id']}
        history.unlink()
        event('thread/deleted',threadId=thread['id'])
    elif method == 'turn/interrupt':
        assert params == keys
        turn['status'] = 'interrupted'
        emit(id=request,result={})
        event('turn/completed',threadId=thread['id'],turn=turn)
        continue
    emit(id=request,result=result)
'''


class CodexWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, predicate):
        deadline = time.monotonic() + 6
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def test_library_lists_only_project_extensions_and_refreshes_while_open(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root/'project'
            profile = root/'profile'
            server = CodexAppServer(root/'codex')

            external_skill = profile/'.codex/skills/.system/built-in/SKILL.md'
            external_skill.parent.mkdir(parents=True)
            external_skill.write_text('---\nname: built-in\n---\n',encoding='utf-8')
            cache = profile/'.codex/plugins/cache/market/figma/1.0'
            cache.mkdir(parents=True)
            (cache/'.mcp.json').write_text('{"mcpServers":{"figma":{}}}',encoding='utf-8')
            (cache.parent/'.codex-remote-plugin-install.json').touch()
            server.home.mkdir()
            (server.home/'config.toml').write_text('[mcp_servers.node_repl]\ncommand="node"\n',encoding='utf-8')

            with (patch('client_tool_library.Path.home',return_value=profile),
                  patch.dict(os.environ,{'CODEX_HOME':str(profile/'.codex'),'APPDATA':str(profile)}),
                  patch('client_workspace.discover_library',
                        side_effect=lambda _: discover_library(project))):
                pane = WorkspacePane(QPixmap(),'',lambda: None,server,lambda: None)
                self.addCleanup(server.close)
                self.addCleanup(pane.close)
                pane.resize(920,700)
                pane.show()
                pane.library_timer.setInterval(25)
                mcp, skills = pane.library_sections
                mcp.header.setChecked(True)
                skills.header.setChecked(True)
                self.assertEqual((mcp.items.count(),skills.items.count()),(0,0))

                package = project/'Extensions/MCP/local-editor'
                package.mkdir(parents=True)
                entry_point = package/'server.mjs'
                entry_point.touch()
                skill = project/'Extensions/Skills/draft/SKILL.md'
                skill.parent.mkdir(parents=True)
                skill.write_text('---\nname: draft-skill\n---\n',encoding='utf-8')
                self.wait(lambda: mcp.items.count()==1 and skills.items.count()==1)
                self.assertEqual(mcp.items.item(0).text(),'local-editor')
                self.assertEqual(skills.items.item(0).text(),'draft-skill')
                self.assertIn(str(skill),skills.items.item(0).toolTip())

                entry_point.unlink()
                package.rmdir()
                skill.unlink()
                self.wait(lambda: mcp.items.count()==0 and skills.items.count()==0)
                self.assertTrue(mcp.header.isChecked() and skills.header.isChecked())

                pane.hide()
                self.assertFalse(pane.library_timer.isActive())
                package.mkdir()
                entry_point.touch()
                pane.show()
                self.assertEqual(mcp.items.item(0).text(),'local-editor')
                pane.close()


    def test_project_sidebar_accepts_folder_drops(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            folders = [root/'项目 A',root/'项目 B']
            for folder in folders:
                folder.mkdir()
            file = root/'笔记.txt'
            file.touch()
            server = CodexAppServer(root/'codex')
            pane = WorkspacePane(QPixmap(),'',lambda: None,server,lambda: None)
            self.addCleanup(server.close)
            self.addCleanup(pane.close)
            pane.resize(920,700)
            pane.show()
            QTest.qWait(15)
            viewport = pane.tree.viewport()
            position = viewport.mapTo(pane,viewport.rect().center())
            mime = QMimeData()
            allowed = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
            left = Qt.MouseButton.LeftButton
            modifiers = Qt.KeyboardModifier.NoModifier

            def enter():
                event = QDragEnterEvent(position,allowed,mime,left,modifiers)
                self.app.sendEvent(pane.windowHandle(),event)
                return event

            def drop():
                event = QDropEvent(QPointF(position),allowed,mime,left,modifiers)
                self.app.sendEvent(pane.windowHandle(),event)
                self.assertTrue(event.isAccepted())
                self.assertEqual(event.dropAction(),Qt.DropAction.CopyAction)

            mime.setUrls([QUrl.fromLocalFile(str(file)),QUrl('https://example.invalid/project')])
            self.assertFalse(enter().isAccepted())
            self.assertEqual(list(pane.projects),[None])
            mime.setUrls([QUrl.fromLocalFile(str(path)) for path in (*folders,file,folders[-1])])
            self.assertTrue(enter().isAccepted())
            self.assertTrue(pane.sidebar.property('folderDropActive'))
            self.app.sendEvent(pane.windowHandle(),QDragLeaveEvent())
            self.assertFalse(pane.sidebar.property('folderDropActive'))
            self.assertEqual(list(pane.projects),[None])
            self.assertTrue(enter().isAccepted())
            drop()
            self.assertEqual(list(pane.projects),[None,*folders])
            self.assertFalse(pane.sidebar.property('folderDropActive'))
            self.assertIs(pane.current_project(),pane.projects[folders[-1]])
            pane.input.setPlainText('保留草稿')
            mime.setUrls([QUrl.fromLocalFile(str(folders[-1]))])
            self.assertTrue(enter().isAccepted())
            drop()
            self.assertEqual(pane.projects[folders[-1]].childCount(),1)
            self.assertEqual(pane.input.toPlainText(),'保留草稿')

    def open_workspace(self, home):
        server = CodexAppServer(home)
        self.addCleanup(server.close)
        pane = WorkspacePane(QPixmap(),'',lambda: None,server,lambda: None)
        self.addCleanup(pane.hide)
        pane.show()
        start = server.process.start
        def launch(*_):
            start(sys.executable,['-X','utf8','-u','-c',SERVER])

        with (patch('client_codex.codex_executable',return_value=sys.executable),
              patch.object(server.process,'start',side_effect=launch)):
            server.start()
        self.wait(lambda:server.connected and not server.pending)
        return server, pane

    def test_new_chat_creates_a_unique_desktop_project_only_when_unassigned(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            desktop = root/'Desktop'
            desktop.mkdir()
            existing = desktop/'Ronova 新聊天'
            existing.write_text('existing file',encoding='utf-8')
            chosen = root/'selected'
            chosen.mkdir()
            server = CodexAppServer(root/'codex')
            self.addCleanup(server.close)
            with patch('client_workspace.QStandardPaths.writableLocation',return_value=str(desktop)):
                pane = WorkspacePane(QPixmap(),'',lambda: None,server,lambda: None)
                self.addCleanup(pane.close)
                new = pane.findChild(QPushButton,'workNewChat')
                self.assertEqual(list(desktop.iterdir()),[existing])
                pane.add_project(chosen)
                new.click()
                self.assertEqual(pane.chat_cwd(pane.active_chat),chosen)
                self.assertEqual(list(desktop.iterdir()),[existing])

                pane.tree.setCurrentItem(pane.projects[None])
                new.click()
                created = desktop/'Ronova 新聊天 (2)'
                self.assertTrue(created.is_dir())
                self.assertEqual(existing.read_text(encoding='utf-8'),'existing file')
                self.assertEqual(pane.chat_cwd(pane.active_chat),created)
                self.assertIs(pane.active_chat.item.parent(),pane.projects[created])
                new.click()
                self.assertEqual(pane.projects[created].childCount(),2)
                self.assertEqual(set(desktop.iterdir()),{existing,created})
                reopened = WorkspacePane(QPixmap(),'',lambda: None,server,lambda: None)
                self.addCleanup(reopened.close)
                self.assertIn(created,reopened.projects)

    def test_first_send_moves_the_unassigned_draft_to_its_desktop_project(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            desktop = root/'Desktop'
            desktop.mkdir()
            server = CodexAppServer(root/'codex')
            server.account = {'type':'chatgpt'}
            self.addCleanup(server.close)
            pane = WorkspacePane(QPixmap(),'',lambda: None,server,lambda: None)
            self.addCleanup(pane.close)
            draft = pane.active_chat
            image = QImage(12,12,QImage.Format.Format_RGB32)
            image.fill(QColor('white'))
            pane.attach_image(image)
            pane.input.setPlainText('检查截图')
            with (patch('client_workspace.QStandardPaths.writableLocation',return_value=''),
                  patch.object(server,'call') as call):
                pane.submit()
            call.assert_not_called()
            self.assertEqual(pane.input.toPlainText(),'检查截图')
            self.assertIn('无法在桌面新建',pane.alert_text.text())
            self.assertIs(draft.item.parent(),pane.projects[None])
            self.assertEqual(len(draft.pictures),1)

            with (patch('client_workspace.QStandardPaths.writableLocation',return_value=str(desktop)),
                  patch.object(server,'call') as call):
                pane.submit()
            method, options, _ = call.call_args.args
            created = desktop/'Ronova 新聊天'
            self.assertTrue(created.is_dir())
            self.assertIs(pane.active_chat,draft)
            self.assertIs(draft.item.parent(),pane.projects[created])
            self.assertIsNotNone(pane.tree.itemWidget(draft.item,1))
            self.assertEqual(draft.turns[0].question,'检查截图')
            self.assertEqual(len(draft.turns[0].pictures),1)
            self.assertEqual((method,options['cwd'],options['sandbox']),
                             ('thread/start',str(created),'workspace-write'))

    def test_chat_backdrop_remains_visible_with_a_draft_and_messages(self):
        with TemporaryDirectory() as directory:
            server = CodexAppServer(Path(directory) / 'codex')
            avatar = QPixmap(1254, 1254)
            avatar.fill(QColor('#30202A'))
            pane = WorkspacePane(avatar, '', lambda: None, server, lambda: None)
            self.addCleanup(pane.close)
            self.addCleanup(server.close)
            pane.resize(920, 700)
            pane.show()
            backdrop = pane.history.parentWidget()
            empty = backdrop.grab().toImage()
            center = empty.width() // 2
            lower = empty.height() * 3 // 4
            self.assertNotEqual(empty.pixelColor(center, lower), empty.pixelColor(2, lower))
            self.assertEqual(empty.pixelColor(center - empty.height() // 2 + 2, 2), empty.pixelColor(2, 2))

            pane.input.setPlainText('保留这条草稿')
            self.assertEqual(backdrop.grab().toImage(), empty)

            pane.active_chat.turns.append(WorkspaceTurn('请检查项目', state='done'))
            pane.render_history()
            pane.refresh_controls()
            self.assertIn('请检查项目', pane.history.toPlainText())
            self.assertEqual(backdrop.grab().toImage().pixelColor(center, lower), empty.pixelColor(center, lower))

    def test_login_marked_screenshot_approval_and_project_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root/'项目'
            project.mkdir()
            server, pane = self.open_workspace(root/'codex')
            self.assertEqual(pane.model_button.text(),'Codex ▼')
            self.assertTrue(pane.account_banner.isVisible())
            with patch('client_codex.QDesktopServices.openUrl',return_value=True) as browser:
                server.login()
                self.wait(lambda:server.signed_in)
                self.assertEqual(browser.call_args.args[0].host(),'auth.openai.com')
            self.assertFalse(pane.account_banner.isVisible())
            self.wait(lambda:pane.catalog_state == 'ready')
            short_bar,short_value = pane.quota.rows[300]
            week_bar,week_value = pane.quota.rows[10080]
            self.wait(lambda:short_value.text() == '72%')
            self.assertEqual((short_bar.value(),week_bar.value(),week_value.text()),(720,600,'60%'))
            pane.quota.refresh()
            server.event.emit('account/rateLimits/updated',{'rateLimits':{'limitId':'codex',
                'primary':{'usedPercent':57.5,'windowDurationMins':None}}})
            self.wait(lambda:pane.quota.pending is None)
            self.assertEqual((short_bar.value(),short_value.text(),week_value.text()),(425,'42.5%','60%'))

            pane.add_project(project)
            chat = pane.active_chat
            menu = QMenu()
            pane.chat_menu(menu,chat)
            self.assertTrue(all(action.isIconVisibleInMenu() and not action.icon().isNull() for action in menu.actions()))
            with patch('client_workspace.QInputDialog.getText',return_value=('截图检查',True)):
                menu.actions()[0].trigger()
            self.assertEqual(pane.chat_title.text(),'截图检查')
            model_menu = pane.model_button.menu()
            model_menu.popup(pane.model_button.mapToGlobal(QPoint(0,0)))
            picker = model_menu.actions()[0].defaultWidget()
            self.assertTrue(picker.slider.isVisible())
            self.assertEqual(picker.slider.maximum(),0)
            self.assertFalse(picker.models.isVisible())
            picker.model_menu.popup(picker.model_button.mapToGlobal(QPoint(0,picker.model_button.height())))
            self.wait(picker.models.isVisible)
            vision = picker.models.item(1)
            QTest.mouseClick(picker.models.viewport(),Qt.MouseButton.LeftButton,
                             pos=picker.models.visualItemRect(vision).center())
            self.assertFalse(picker.model_menu.isVisible())
            self.assertTrue(model_menu.isVisible())
            self.assertEqual(picker.slider.maximum(),1)
            self.assertEqual(chat.effort,'high')
            picker.slider.resize(260,36)
            QTest.mouseClick(picker.slider,Qt.MouseButton.LeftButton,pos=QPoint(246,18))
            self.assertEqual(chat.effort,'xhigh')
            picker.reset.click()
            self.assertEqual(chat.effort,'high')
            QTest.keyClick(picker.slider,Qt.Key.Key_Right)
            self.assertEqual(chat.effort,'xhigh')
            model_menu.close()
            menu.clear()
            pane.permission_menu(menu)
            permissions = menu.actions()[0].defaultWidget()
            buttons = permissions.findChildren(QPushButton,'workPermissionRow')
            self.assertTrue(next(button for button in buttons if button.property('permission') == 'manual').property('chosen'))
            next(button for button in buttons if button.property('permission') == 'review').click()
            self.assertIn('Fixture Vision · 极高',pane.model_button.text())
            image = QImage(80,60,QImage.Format.Format_RGB32)
            image.fill(QColor('#203060'))
            picture = Picture(image,(Mark('line',((4,10),(44,10))),))
            pane.add_picture(picture)
            second_image = image.copy(); second_image.fill(QColor('#4A3060'))
            pane.attach_image(second_image)
            pictures = pane.pictures
            self.wait(lambda: pane.attachment.viewport().height() >= pane.attachment_row.sizeHint().height())
            pane.input.setPlainText('检查截图')
            pane.submit()
            self.wait(pane.approval_card.isVisible)
            self.assertIn('Get-Location',pane.approval_detail.toPlainText())
            self.assertIn(str(project),pane.approval_detail.toPlainText())
            pane.new_chat(chat.item.parent())
            self.assertFalse(pane.approval_card.isVisible())
            self.assertFalse(chat.item.icon(0).isNull())
            pane.tree.setCurrentItem(chat.item)
            pane.allow.click()
            self.wait(lambda:pane.request is None)
            self.assertIn('处理完成',pane.history.toPlainText())
            self.assertNotIn('cwd result',pane.history.toPlainText())
            row = pane.history.rows[chat.turns[-1]]
            files = row.changed_files
            trace = row.execution.button
            self.assertEqual(files.button.objectName(),trace.objectName())
            self.assertEqual(files.button.text(),'修改的文件（2）')
            self.assertTrue(files.button.isVisible())
            self.assertFalse(files.detail.isVisible())
            self.assertNotIn('src/main.py',pane.history.toPlainText())
            files.button.click()
            self.assertTrue(files.detail.isVisible())
            self.assertEqual(files.detail.toPlainText().splitlines(),['src/main.py','docs/说明.md'])
            files.button.click()
            self.assertFalse(files.detail.isVisible())
            self.assertFalse(trace.isChecked())
            trace.click()
            self.assertIn('cwd result',pane.history.toPlainText().split('处理完成',1)[1])
            self.assertEqual(pane.context_button.text(),'')
            self.assertEqual(pane.context_button.toolTip(),'已占用 15%')
            menu.clear()
            pane.context_menu(menu)
            self.assertTrue(any('1,200 / 8,000' in action.text() for action in menu.actions()))
            menu.clear()
            pane.composer_menu(menu)
            self.assertEqual([action.text() for action in menu.actions()], ['压缩上下文','计划模式'])
            capture = pane.findChild(QPushButton, 'workCapture')
            self.assertLess(pane.add_action.geometry().right(), capture.x())
            menu.actions()[0].trigger()
            self.wait(lambda:not chat.compacting)
            self.assertEqual(pane.context_button.toolTip(),'已占用 2.5%')
            self.assertTrue(chat.item.icon(0).isNull())
            stored = json.loads((server.home/'fixture.json').read_text(encoding='utf-8'))
            self.assertEqual(stored[0]['approvalsReviewer'],'auto_review')
            text_input, *image_inputs = stored[1][0]['items'][0]['content']
            self.assertEqual(text_input, {'type':'text','text':'检查截图','text_elements':[]})
            self.assertEqual([item['type'] for item in image_inputs], ['localImage','localImage'])
            self.assertEqual([Path(item['path']).read_bytes() for item in image_inputs],
                             [item.png for item in pictures])

            menu.clear()
            pane.composer_menu(menu)
            menu.actions()[1].trigger()
            self.assertTrue(chat.plan_mode)
            self.assertEqual(pane.status.text(), '计划模式')
            pane.input.setPlainText('制定修改计划')
            pane.submit()
            self.wait(pane.approval_card.isVisible)
            self.assertEqual(pane.allow.text(), '回答问题')
            def answer(dialog):
                dialog.findChild(QComboBox).setCurrentIndex(0)
                return QDialog.DialogCode.Accepted
            with patch('client_workspace.ProjectDialog.exec',new=answer):
                pane.allow.click()
            self.wait(lambda:pane.request is None)
            self.assertIn('第一步：检查界面',pane.history.toPlainText())
            menu.clear()
            pane.composer_menu(menu)
            self.assertTrue(menu.actions()[1].isChecked())
            menu.actions()[1].trigger()
            self.assertFalse(chat.plan_mode)
            pane.set_permission(chat, 'read')
            pane.input.setPlainText('请只读检查')
            pane.submit()
            self.wait(lambda:pane.request is None)
            self.assertIn('只读模式完成',pane.history.toPlainText())
            server.close()
            pane.hide()

            restored_server, restored = self.open_workspace(server.home)
            self.assertIn(project,restored.projects)
            restored.tree.setCurrentItem(restored.projects[project].child(0))
            self.wait(lambda:restored.active_chat.history_loaded)
            self.assertEqual(restored.chat_title.text(),'截图检查')
            with patch('client_workspace.QInputDialog.getText',return_value=('更新截图检查',True)):
                restored.rename_chat(restored.active_chat)
            self.wait(lambda:restored.chat_title.text() == '更新截图检查' and not restored.active_chat.renaming)
            self.assertIn('处理完成',restored.history.toPlainText())
            saved_files = restored.history.rows[restored.turns[0]].changed_files
            self.assertEqual(saved_files.button.text(),'修改的文件（2）')
            self.assertFalse(saved_files.button.isChecked())
            saved_files.button.click()
            self.assertEqual(saved_files.detail.toPlainText().splitlines(),['src/main.py','docs/说明.md'])
            self.assertEqual(len(restored.turns[0].pictures), 2)
            for saved, original in zip(restored.turns[0].pictures, pictures):
                self.assertEqual(saved.preview.convertToFormat(original.preview.format()), original.preview)
            restored.input.setPlainText('再运行一次')
            restored.submit()
            self.wait(restored.approval_card.isVisible)
            restored.reject.click()
            self.wait(lambda:restored.request is None)
            self.assertIn('操作已拒绝',restored.history.toPlainText())
            self.assertTrue(restored.history.rows[restored.turns[-1]].changed_files.isHidden())
            restored.input.setPlainText('本轮会停止')
            restored.submit()
            self.wait(restored.approval_card.isVisible)
            restored.stop.click()
            self.wait(lambda:restored.request is None)
            self.assertEqual(restored.turns[-1].state,'stopped')
            menu.clear()
            restored.permission_menu(menu)
            permissions = menu.actions()[0].defaultWidget()
            buttons = permissions.findChildren(QPushButton,'workPermissionRow')
            next(button for button in buttons if button.property('permission') == 'read').click()
            restored.input.setPlainText('请只读检查')
            restored.submit()
            self.wait(lambda:restored.request is None)
            self.assertIn('只读模式完成',restored.history.toPlainText())
            project_item = restored.projects[project]
            saved = restored.active_chat
            restored.new_chat(project_item)
            restored.delete_selected_chat()
            self.assertIs(restored.active_chat,saved)
            with patch('client_workspace.QMessageBox.exec',return_value=QMessageBox.StandardButton.Yes):
                restored.delete_selected_chat()
            self.wait(lambda:restored.active_chat is None)
            self.assertEqual(project_item.childCount(),0)
            self.assertFalse((restored_server.home/'fixture.json').exists())
            self.assertTrue(all(not Path(item['path']).exists() for item in image_inputs))
            self.assertFalse(restored.input.isEnabled())
            restored_server.close()

    def test_delete_project_cleans_older_cache_and_preserves_source_folder(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project, other_project = root/'project', root/'other'
            project.mkdir()
            other_project.mkdir()
            source = project/'source.txt'
            source.write_text('keep the original',encoding='utf-8')
            home = root/'codex'
            media = home/'screenshots'
            media.mkdir(parents=True)
            older_image = media/('a'*32+'.png')
            unrelated_image = media/('b'*32+'.png')
            outside_image = project/('c'*32+'.png')
            shared_image = media/('d'*32+'.png')
            for image in (older_image,unrelated_image,outside_image,shared_image):
                image.write_bytes(b'fixture screenshot')
            turns = [{'id':'saved-turn','status':'completed','items':[{'type':'userMessage','id':'input',
                     'content':[{'type':'localImage','path':str(image)} for image in (older_image,outside_image,shared_image)]}]}]
            (home/'fixture.json').write_text(json.dumps([{'id':'stored','cwd':str(project)},turns]),encoding='utf-8')
            archived_turns = [{'id':'archive-turn','status':'completed','items':[{'type':'userMessage','id':'input',
                              'content':[{'type':'localImage','path':str(shared_image)}]}]}]
            (home/'fixture-archived.json').write_text(json.dumps(
                [{'id':'archived','cwd':str(other_project)},archived_turns]),encoding='utf-8')
            server,pane = self.open_workspace(home)
            project_item = pane.projects[project]
            pane.new_chat(project_item)
            pane.input.setPlainText('要一起删除的草稿')
            pane.add_project(other_project)
            pane.input.setPlainText('保留另一项目的草稿')
            menu = QMenu()
            pane.project_menu(menu,project_item)
            self.assertTrue(all(action.isIconVisibleInMenu() and not action.icon().isNull() for action in menu.actions()))
            def rename(dialog):
                dialog.findChild(QLineEdit).setText('另一个名称')
                return QDialog.DialogCode.Accepted
            with patch('client_workspace.ProjectDialog.exec',new=rename):
                menu.actions()[0].trigger()
            self.assertEqual(project_item.text(0),'另一个名称')
            self.assertEqual(pane.project_cwd(project_item),project)
            with patch('client_workspace.ProjectDialog.exec',return_value=QDialog.DialogCode.Accepted):
                menu.actions()[1].trigger()
            self.wait(lambda:project not in pane.projects)
            self.assertEqual(pane.input.toPlainText(),'保留另一项目的草稿')
            self.assertFalse((home/'fixture.json').exists())
            self.assertFalse(older_image.exists())
            self.assertTrue(unrelated_image.exists() and outside_image.exists())
            self.assertTrue(shared_image.exists())
            self.assertEqual(source.read_text(encoding='utf-8'),'keep the original')
            server.close()

    def test_rename_and_delete_unassigned_project(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)/'codex'
            workspace = home/'workspace'
            workspace.mkdir(parents=True)
            history = home/'fixture.json'
            history.write_text(json.dumps([{'id':'unassigned','cwd':str(workspace)},[]]),encoding='utf-8')
            server,pane = self.open_workspace(home)
            unassigned = pane.projects[None]
            self.assertIsNotNone(pane.tree.itemWidget(unassigned,1))
            menu = QMenu()
            pane.project_menu(menu,unassigned)
            def rename(dialog):
                dialog.findChild(QLineEdit).setText('随手记')
                return QDialog.DialogCode.Accepted
            with patch('client_workspace.ProjectDialog.exec',new=rename):
                menu.actions()[0].trigger()
            self.assertEqual(unassigned.text(0),'随手记')
            server.close()
            pane.hide()

            server,pane = self.open_workspace(home)
            unassigned = pane.projects[None]
            self.assertEqual(unassigned.text(0),'随手记')
            self.assertIn('unassigned',pane.codex_chats)
            pane.tree.setCurrentItem(unassigned)
            self.wait(lambda:pane.active_chat is not None and not pane.active_chat.loading_history)
            menu.clear()
            pane.project_menu(menu,unassigned)
            with patch('client_workspace.ProjectDialog.exec',return_value=QDialog.DialogCode.Accepted):
                menu.actions()[1].trigger()
            self.wait(lambda:None not in pane.projects)
            self.assertIsNone(pane.active_chat)
            self.assertFalse(history.exists())
            server.close()
            pane.hide()

            server,pane = self.open_workspace(home)
            self.assertNotIn(None,pane.projects)
            self.assertFalse(pane.input.isEnabled())
            desktop = home.parent/'Desktop'
            desktop.mkdir()
            with patch('client_workspace.QStandardPaths.writableLocation',return_value=str(desktop)):
                pane.findChild(QPushButton,'workNewChat').click()
            created = desktop/'Ronova 新聊天'
            self.assertTrue(created.is_dir())
            self.assertNotIn(None,pane.projects)
            self.assertIs(pane.active_chat.item.parent(),pane.projects[created])
            server.close()
