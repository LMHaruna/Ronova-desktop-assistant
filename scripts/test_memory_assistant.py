"""Natural-language memory edits, including concurrency and UI behavior."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from client_companion import SemanticAnalyzer
from client_memory import MemoryStore
import test_model_providers as fixtures


class MemoryEditStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory()
        self.store=MemoryStore(Path(self.directory.name)/'memory.sqlite3')
        self.first=self.store.save('我喜欢茶','preference')
        self.second=self.store.save('我喜欢安静','preference')

    def tearDown(self): self.store.close(); self.directory.cleanup()

    def snapshot(self):
        state=self.store.snapshot()
        return state['memory_epoch'],[(row['id'],row['updated']) for row in state['memories']]

    def test_update_is_saved_and_versioned(self):
        epoch,versions=self.snapshot()
        change={'operation':'update','id':self.first,'category':'preference','content':'我喜欢咖啡'}
        self.assertEqual(self.store.apply_requested_edits([change],epoch,versions,'我现在喜欢咖啡'),1)
        records={row['id']:row for row in self.store.snapshot()['memories']}
        self.assertEqual(records[self.first]['content'],'我喜欢咖啡')
        self.assertEqual(records[self.second]['content'],'我喜欢安静')
        self.assertEqual(self.store.versions(self.first)[0]['reason'],'assisted_update')

    def test_invalid_multiple_edits_roll_back_every_change(self):
        epoch,versions=self.snapshot()
        changes=[{'operation':'update','id':self.first,'category':'preference','content':'我喜欢咖啡'},
                 {'operation':'update','id':self.second,'category':'preference','content':'我喜欢咖啡'}]
        with self.assertRaises(ValueError):
            self.store.apply_requested_edits(changes,epoch,versions,'修改偏好')
        self.assertEqual({row['content'] for row in self.store.snapshot()['memories']},{'我喜欢茶','我喜欢安静'})

    def test_stale_snapshot_is_rejected(self):
        epoch,versions=self.snapshot()
        self.store.save('我喜欢绿茶','preference',self.first)
        change={'operation':'delete','id':self.first,'category':'','content':''}
        with self.assertRaisesRegex(ValueError,'发生变化'):
            self.store.apply_requested_edits([change],epoch,versions,'忘掉茶')
        self.assertEqual(len(self.store.snapshot()['memories']),2)

    def test_sensitive_information_is_rejected(self):
        epoch,versions=self.snapshot()
        change={'operation':'add','id':'','category':'fact','content':'我的密码是123'}
        with self.assertRaises(ValueError): self.store.apply_requested_edits([change],epoch,versions,'请记住')
        self.assertEqual(len(self.store.snapshot()['memories']),2)

    def test_credentials_are_not_saved_in_edit_evidence(self):
        epoch,versions=self.snapshot()
        change={'operation':'update','id':self.first,'category':'preference','content':'我喜欢咖啡'}
        self.store.apply_requested_edits([change],epoch,versions,'我的密码是123，顺便改成咖啡')
        self.assertEqual(self.store.versions(self.first)[0]['evidence'],'')


class MemoryEditProtocolTests(unittest.TestCase):
    def test_model_receives_prior_dialogue_and_real_records(self):
        client=Mock()
        client.reply.return_value=json.dumps({'reply':'已更正。','changes':[
            {'operation':'update','id':'abc','category':'preference','content':'我喜欢咖啡'}]},ensure_ascii=False)
        analyzer=SemanticAnalyzer('gpt',None,client=client)
        result=analyzer.revise_memory('改成咖啡',[{'id':'abc','category':'preference','content':'我喜欢茶'}],
                                      threading.Event(),[{'user':'修改饮品','assistant':'想改成什么？'}])
        self.assertEqual(result['changes'][0]['id'],'abc')
        payload=json.loads(client.reply.call_args.args[0])
        self.assertEqual(payload['近期修改对话'][0]['assistant'],'想改成什么？')
        self.assertIn('不虚构',client.history[0]['content'])

    def test_delete_requires_explicit_user_request(self):
        client=Mock(); client.reply.return_value=json.dumps({'reply':'已删除','changes':[
            {'operation':'delete','id':'abc','category':'','content':''}]})
        analyzer=SemanticAnalyzer('gpt',None,client=client)
        with self.assertRaisesRegex(ValueError,'明确说明'):
            analyzer.revise_memory('请看看记忆',[],threading.Event())


class FakeMemoryEditor:
    def __init__(self): self.requests=[]
    def revise_memory(self, instruction, memories, cancel, history):
        self.requests.append((instruction,history))
        return {'reply':'我把饮品偏好改成了咖啡。','changes':[
            {'operation':'update','id':memories[0]['id'],'category':'preference','content':'我喜欢咖啡'}]}
    def summarize(self, memories, cancel): return '用户喜欢咖啡。'
    def cancel(self): pass
    def close(self): pass


class MemoryEditUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixtures.ProviderUI.setUpClass.__func__(cls)
    setUp=fixtures.ProviderUI.setUp
    tearDown=fixtures.ProviderUI.tearDown

    def test_request_changes_memory_and_refreshes_paragraph(self):
        self.controller.memory.save('我喜欢茶','preference')
        editor=FakeMemoryEditor()
        self.controller.companion.factory=lambda *_:editor
        pane=self.window.settings_pane.memory_page
        pane.refresh(force=True)
        self.assertNotIn('添加补充',[item.text() for item in pane.home.findChildren(QPushButton)])
        pane.memory_request.setPlainText('我现在喜欢咖啡')
        pane.request_change()
        deadline=time.monotonic()+5
        while pane.pending and time.monotonic()<deadline:
            pane.refresh(); QTest.qWait(10)
        self.assertIsNone(pane.pending)
        snapshot=self.controller.memory.snapshot()
        self.assertEqual(snapshot['memories'][0]['content'],'我喜欢咖啡')
        self.assertEqual(snapshot['memory_summary'],'用户喜欢咖啡。')
        self.assertIn('已更新记忆',pane.memory_reply.text())
        self.assertEqual(pane.memory_request.toPlainText(),'')
        self.assertEqual(editor.requests[0][0],'我现在喜欢咖啡')

    def test_clarification_keeps_memory_and_dialogue_context(self):
        self.controller.memory.save('我喜欢茶','preference')
        editor=FakeMemoryEditor()
        editor.revise_memory=lambda instruction,memories,cancel,history: {
            'reply':'想改成哪种饮品？','changes':[]}
        self.controller.companion.factory=lambda *_:editor
        pane=self.window.settings_pane.memory_page
        pane.memory_request.setPlainText('修改我的饮品偏好')
        pane.request_change()
        deadline=time.monotonic()+5
        while pane.pending and time.monotonic()<deadline:
            pane.refresh(); QTest.qWait(10)
        self.assertIsNone(pane.pending)
        self.assertEqual(self.controller.memory.snapshot()['memories'][0]['content'],'我喜欢茶')
        self.assertIn('尚未修改记忆',pane.memory_reply.text())
        self.assertEqual(pane.request_history[0]['assistant'],'想改成哪种饮品？')

    def test_stale_response_preserves_new_memory_and_request(self):
        self.controller.memory.save('我喜欢茶','preference')
        editor=FakeMemoryEditor()
        def stale(instruction, memories, cancel, history):
            self.controller.memory.save('我喜欢绿茶','preference',memories[0]['id'])
            return {'reply':'已更正。','changes':[
                {'operation':'update','id':memories[0]['id'],'category':'preference','content':'我喜欢咖啡'}]}
        editor.revise_memory=stale
        self.controller.companion.factory=lambda *_:editor
        pane=self.window.settings_pane.memory_page
        pane.memory_request.setPlainText('我现在喜欢咖啡')
        pane.request_change()
        deadline=time.monotonic()+5
        while pane.pending and time.monotonic()<deadline:
            pane.refresh(); QTest.qWait(10)
        self.assertIsNone(pane.pending)
        self.assertEqual(self.controller.memory.snapshot()['memories'][0]['content'],'我喜欢绿茶')
        self.assertIn('发生变化',pane.status_label.text())
        self.assertEqual(pane.memory_request.toPlainText(),'我现在喜欢咖啡')


if __name__=='__main__': unittest.main()
