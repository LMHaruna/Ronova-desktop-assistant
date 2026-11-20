"""Continuous, grounded long-term memory and its lifecycle."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock

from PySide6.QtWidgets import QLabel

from client_companion import CompanionWorker, SemanticAnalyzer, clean_summary
from client_memory import MemoryStore
from client_relationship import parse_analysis
from client_panels import SettingsRow
import test_model_providers as fixtures
from test_companion import packet


class SummaryAnalyzer:
    def __init__(self):
        self.received=[]
        self.fail=False

    def analyze(self, text, reply, memories, cancel):
        value=packet(text,memories=[('preference.饮料','preference',text)])
        return parse_analysis(json.dumps(value,ensure_ascii=False),text)

    def summarize(self, memories, cancel):
        self.received.append([row['content'] for row in memories])
        if self.fail: raise RuntimeError('fixture')
        return '用户喜欢喝茶，也喜欢安静。'

    def cancel(self): pass
    def close(self): pass


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory()
        self.path=Path(self.directory.name)/'memory.sqlite3'
        self.store=MemoryStore(self.path)
        self.analyzer=SummaryAnalyzer()
        self.worker=CompanionWorker(self.store,factory=lambda *_:self.analyzer)

    def tearDown(self):
        self.worker.close(); self.store.close(); self.directory.cleanup()

    def enqueue(self, text, identifier='turn'):
        profile=self.store.profile()
        ticket={key:profile[key] for key in ('memory_epoch','relationship_epoch')}
        self.worker.enqueue({'id':identifier,'backend':'gpt','text':text,'reply':'角色的回复'},None,ticket)
        self.assertTrue(self.worker.wait_idle(5))

    def test_summarizes_only_accepted_records_and_persists(self):
        self.store.save('我喜欢安静')
        self.enqueue('我喜欢喝茶')
        value=self.store.snapshot()
        self.assertEqual(set(self.analyzer.received[0]),{'我喜欢安静','我喜欢喝茶'})
        self.assertEqual(value['memory_summary'],'用户喜欢喝茶，也喜欢安静。')
        self.assertIn(value['memory_summary'],self.store.context('你好'))
        self.store.close(); self.store=MemoryStore(self.path)
        self.assertEqual(self.store.snapshot()['memory_summary'],value['memory_summary'])

    def test_summary_is_invalidated_on_record_change_and_clear(self):
        self.enqueue('我喜欢喝茶')
        snapshot=self.store.snapshot()
        identifier=snapshot['memories'][0]['id']
        expected=[(row['id'],row['updated']) for row in snapshot['memories']]
        self.store.save('我喜欢咖啡','preference',identifier)
        self.assertFalse(self.store.save_summary('过期内容',snapshot['memory_epoch'],expected))
        self.assertEqual(self.store.snapshot()['memory_summary'],'')
        self.store.clear()
        self.assertEqual(self.store.snapshot()['memory_summary'],'')
        self.assertEqual(self.store.snapshot()['memories'],[])

    def test_disabled_or_failed_generation_does_not_lose_facts(self):
        self.store.configure(auto_memory=False)
        self.enqueue('我喜欢喝茶','disabled')
        self.assertEqual(self.analyzer.received,[])
        self.store.configure(auto_memory=True)
        self.analyzer.fail=True
        self.enqueue('我喜欢喝茶','failed')
        self.assertEqual(self.store.snapshot()['memories'][0]['content'],'我喜欢喝茶')
        self.assertEqual(self.store.snapshot()['memory_summary'],'')
        self.assertIn('整理未完成',self.worker.snapshot()['message'])

    def test_protocol_uses_saved_facts_and_one_paragraph(self):
        client=Mock()
        client.reply.return_value='用户喜欢茶，\n也喜欢安静。'
        analyzer=SemanticAnalyzer('gpt',None,client=client)
        self.assertEqual(analyzer.summarize([{'content':'我喜欢茶'}],threading.Event()),'用户喜欢茶， 也喜欢安静。')
        self.assertEqual(json.loads(client.reply.call_args.args[0]),{'已保存的用户记忆':['我喜欢茶']})
        self.assertIn('不虚构',client.history[0]['content'])
        for invalid in ('- 喜欢茶\n- 喜欢咖啡','1. 喜欢茶','```摘要```','密码是123'):
            with self.subTest(invalid=invalid),self.assertRaises(ValueError): clean_summary(invalid)


class SummaryUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls): fixtures.ProviderUI.setUpClass.__func__(cls)
    setUp=fixtures.ProviderUI.setUp
    tearDown=fixtures.ProviderUI.tearDown

    def test_home_displays_one_continuous_memory_without_record_rows(self):
        store=self.controller.memory
        store.save('我喜欢茶'); store.save('我喜欢安静')
        snapshot=store.snapshot()
        expected=[(row['id'],row['updated']) for row in snapshot['memories']]
        self.assertTrue(store.save_summary('用户喜欢喝茶，也喜欢安静。',snapshot['memory_epoch'],expected))
        pane=self.window.settings_pane.memory_page
        pane.refresh(force=True)
        self.assertEqual(pane.list_layout.count(),1)
        card=pane.list_layout.itemAt(0).widget()
        self.assertFalse(card.findChildren(SettingsRow))
        self.assertEqual([label.text() for label in card.findChildren(QLabel)],['用户喜欢喝茶，也喜欢安静。'])
        pane.manage_records()
        self.assertTrue(pane.manage_button.isEnabled())
        self.assertEqual(pane.record_position.text(),'1 / 2')
        pane.step_record(1)
        self.assertEqual(pane.record_position.text(),'2 / 2')
        pane.content.insertPlainText('补充')
        self.assertFalse(pane.previous_record.isEnabled())


if __name__=='__main__': unittest.main()
