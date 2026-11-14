"""Incoming call admission, persistence, consent, cancellation and real Qt UI."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock, patch

ROOT=Path(os.environ.get('RONOVA_ASSET_ROOT',Path(__file__).resolve().parents[1]))
CODE=Path(os.environ.get('RONOVA_CODE_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(CODE),str(CODE/'scripts'),str(ROOT),str(ROOT/'scripts'),str(ROOT/'runtime/client-tools')]
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from client_calls import CallStore, SHANGHAI, parse_decision, first_line, opening_key, varied_opening
from chat_client import ChatError
from client_store import Preferences, HistoryStore
from client_controller import DesktopController
from client_fixtures import FakeCall, fake_router
from local_voice import SpeechJob
from client_app import ClientWindow
from client_panels import ronova_status
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt

PROPOSAL={'action':'call','topic':'昨天的项目进展','reason':'想问问你昨天的事','opening':'凡人，昨天那件事，后来怎么样了？'}
TEXT={'action':'text','topic':PROPOSAL['topic'],'message':'昨天那个项目推进得怎么样？要一起梳理下后续吗？'}


class Clock:
    def __init__(self): self.now=datetime(2026,10,13,12,tzinfo=SHANGHAI).timestamp()
    def __call__(self): return self.now
    def advance(self,seconds): self.now+=seconds


class Planner:
    def __init__(self,result=PROPOSAL):
        self.result=result; self.calls=0; self.entered=threading.Event(); self.release=None
    def decide(self,context,cancel):
        self.calls+=1; self.context=context; self.entered.set()
        if self.release: self.release.wait(3)
        return self.result
    def cancel(self):
        if self.release: self.release.set()
    def close(self): pass


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(); self.clock=Clock()
        self.path=Path(self.directory.name)/'calls.sqlite3'; self.store=CallStore(self.path)
    def tearDown(self): self.store.close(); self.directory.cleanup()
    def test_strict_model_decision_and_no_call(self):
        self.assertIsNone(parse_decision('{"action":"wait"}'))
        self.assertEqual(parse_decision(PROPOSAL)['opening'],PROPOSAL['opening'])
        self.assertEqual(parse_decision(TEXT),TEXT)
        for value in ({'action':'bad'}, {'action':1}, dict(PROPOSAL,opening=''),
                      dict(PROPOSAL,opening='x'*121),dict(PROPOSAL,reason='a\n'),
                      dict(TEXT,topic='!?'),dict(TEXT,message='x'*201)):
            with self.assertRaises((ValueError,TypeError)): parse_decision(value)
    def test_legacy_timer_preferences_require_fresh_opt_in(self):
        path=self.path.with_suffix('.json')
        path.write_text('{"proactive_calls":true,"call_start_hour":3,"call_daily_limit":2}',encoding='utf-8')
        prefs=Preferences.load(path)
        self.assertFalse(prefs.proactive_text or prefs.proactive_calls)
        prefs=replace(prefs,proactive_calls=True); prefs.save(path)
        self.assertEqual(Preferences.load(path),prefs)
        self.assertNotIn('call_daily_limit',path.read_text(encoding='utf-8'))
    def test_events_and_topics_persist_across_restart(self):
        self.assertTrue(self.store.remember_event('one'))
        self.store.create('qwen',parse_decision(PROPOSAL),'one',False,self.clock())
        self.store.close(); self.store=CallStore(self.path)
        self.assertFalse(self.store.remember_event('one'))
        self.assertFalse(self.store.claim_topic('昨天的项目进展！'))
        self.assertEqual(self.store.topics(),[PROPOSAL['topic']])
    def test_restart_does_not_revive_ringing_and_preserves_snooze(self):
        a=self.store.create('qwen',parse_decision(PROPOSAL),'a',False,self.clock())
        b=self.store.create('qwen',parse_decision(PROPOSAL),'b',True,self.clock())
        self.store.finish(b['id'],'snoozed',self.clock(),due=self.clock()+1800,expires=self.clock()+14400)
        self.store.close(); self.store=CallStore(self.path)
        self.assertEqual(self.store.find(a['id'])['state'],'missed')
        self.assertEqual(self.store.pending()[0]['id'],b['id'])
    def test_invalid_preferences(self):
        for p in (Preferences(proactive_calls=1),Preferences(proactive_text='true')):
            with self.assertRaises(ValueError): p.validate()
    def test_test_calls_do_not_repeat_recent_first_sentences_even_after_restart(self):
        previous=[]
        for index in range(45):
            if index==22:
                self.store.close(); self.store=CallStore(self.path)
            row=self.store.create('qwen',{'reason':'测试来电','opening':''},str(index),True,self.clock())
            key=opening_key(row['opening'])
            self.assertNotIn(key,previous[-20:]); previous.append(key)
            self.assertTrue(row['opening'].strip()); self.assertLessEqual(len(row['opening']),120)
    def test_model_duplicate_first_sentence_gets_new_greeting_and_preserves_topic(self):
        proposal=parse_decision(PROPOSAL)
        one=self.store.create('qwen',proposal,'one',False,self.clock())
        two=self.store.create('qwen',dict(proposal,topic='另一个项目'),'two',False,self.clock())
        self.assertNotEqual(opening_key(one['opening']),opening_key(two['opening']))
        self.assertIn(PROPOSAL['opening'],two['opening'])
    def test_punctuation_changes_do_not_bypass_opening_variety(self):
        self.assertEqual(opening_key('是我，若娜瓦。'),opening_key('是 我,若娜瓦！'))
        text=varied_opening('是 我,若娜瓦！有件事想聊聊。',['是我，若娜瓦。'])
        self.assertNotEqual(opening_key(text),opening_key('是我，若娜瓦。'))
        self.assertIn('有件事想聊聊。',text)
    def test_long_repeated_opening_keeps_whole_sentence_and_bounds_length(self):
        text='想和你说几句话。'+'想'*100+'。'
        varied=varied_opening(text,[text])
        self.assertLessEqual(len(varied),120); self.assertTrue(varied.endswith('。'))
    def test_status_shows_character_activity_and_mood(self):
        profile={'angry':False,'stage':'acquaintance'}
        self.assertEqual(ronova_status({},profile,datetime(2026,10,13,22,tzinfo=SHANGHAI)),'加班中')
        self.assertEqual(ronova_status({},profile,datetime(2026,10,13,10,tzinfo=SHANGHAI)),'忙碌')
        self.assertEqual(ronova_status({},profile,datetime(2026,10,13,19,tzinfo=SHANGHAI)),'休息中')
        self.assertEqual(ronova_status({},dict(profile,stage='bond')),'开心')
        self.assertEqual(ronova_status({'call_requested':True},profile),'陪你聊天')
        self.assertEqual(ronova_status({'incoming':{'id':'a'}},profile),'想找你聊聊')
        self.assertEqual(ronova_status({},dict(profile,angry=True)),'想静一静')


class IncomingBase(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(); self.clock=Clock(); self.planner=Planner()
        self.router,self.chat,self.voice=fake_router()
        # Ringtone/device behavior has its own suite. A temporary controller
        # has no ringtone asset; a real ringer emits an unrelated failure toast.
        self.ringtone=Mock()
        self.ringtone.stop.return_value=True
        self.controller=DesktopController(self.directory.name,router=self.router,call_factory=FakeCall,
            call_planner_factory=lambda *args:self.planner,call_clock=self.clock,
            ringtone_factory=lambda *args,**kwargs:self.ringtone)
        self.calls=self.controller.incoming_calls
    def tearDown(self): self.controller.close().result(5); self.directory.cleanup()
    def ring(self):
        identifier=self.controller.test_incoming_call().result(3)
        self.assertIsNotNone(identifier); return identifier
    def seed(self):
        self.controller.store.begin('seed','qwen','昨天说的事情')
        self.controller.store.update('seed','后来如何','completed')
    def enable(self, *, text=False, calls=True):
        self.controller.apply_preferences(replace(self.controller.preferences,
            proactive_text=text,proactive_calls=calls)).result(3)
    def offer(self, message='昨天的项目现在有新进展', *, source='text'):
        self.calls.offer(source,message,'qwen')
    def tick(self,seconds=1): self.clock.advance(seconds); self.controller.poll()
    def finish_decision(self):
        if self.calls.future: self.calls.future.result(3)
        self.tick()


class IncomingTests(IncomingBase):
    def test_no_network_or_microphone_from_old_history_and_elapsed_time(self):
        self.enable(text=True); self.seed()
        for _ in range(60): self.tick(60)
        self.assertEqual(self.planner.calls,0); self.assertFalse(self.controller.call.active)
    def test_real_eligible_decision_rings_without_capture(self):
        self.enable(); self.offer(); self.tick(6); self.finish_decision()
        row=self.controller.snapshot()['incoming']; self.assertEqual(row['reason'],PROPOSAL['reason'])
        self.assertEqual(self.planner.calls,1); self.assertFalse(self.controller.call.active)
        self.assertEqual(self.controller.call.starts,[])
    def test_call_waits_for_idle_and_voice_readiness_without_asking_model_again(self):
        self.enable(); self.offer(); self.tick(6)
        self.calls.future.result(3)
        self.clock.advance(1)
        self.controller.poll(interaction_busy=True)
        self.assertIsNone(self.calls.snapshot())
        with patch.object(self.controller,'voice_load_state',return_value={'voice_ready':False}):
            self.tick()
            self.assertIsNone(self.calls.snapshot())
        self.tick()
        self.assertEqual(self.calls.snapshot()['reason'],PROPOSAL['reason'])
        self.assertEqual(self.planner.calls,1)
        self.assertEqual(self.controller.call.starts,[])
    def test_deferred_call_expires_with_its_trigger(self):
        self.enable(); self.offer(); self.tick(6)
        self.calls.future.result(3)
        for _ in range(2):
            self.clock.advance(57)
            self.controller.poll(interaction_busy=True)
        self.tick()
        self.assertIsNone(self.calls.snapshot())
        self.assertIsNone(self.calls.future)
    def test_planner_receives_recent_first_lines_and_test_call_is_local(self):
        a=self.ring(); opening=self.calls.snapshot()['opening']; self.controller.decline_call(a).result(3)
        self.enable(); self.offer(); self.tick(6); self.finish_decision()
        self.assertIn(first_line(opening),self.planner.context['recent_opening_lines'])
        self.assertEqual(self.chat.calls,0); self.assertEqual(self.controller.call.starts,[])
    def test_wait_result_and_repeated_event_do_not_retry(self):
        self.enable(); self.planner.result={'action':'wait'}; self.offer(); self.tick(6); self.finish_decision()
        self.assertIsNone(self.controller.snapshot()['incoming']); self.tick(30)
        self.offer('昨天的项目现在有新进展！',source='voice'); self.tick(6)
        self.assertIsNone(self.calls.future)
        self.assertEqual(self.planner.calls,1)
    def test_responding_defers_decision(self):
        self.enable(); self.chat.release=threading.Event()
        self.controller.submit('继续').result(3); self.assertTrue(self.chat.entered.wait(2))
        self.offer(); self.tick(6); self.assertEqual(self.planner.calls,0)
        self.chat.release.set(); self.controller.pipeline.wait_finished(3)
    def test_active_call_and_oath_defer_decision(self):
        self.enable(); self.controller.start_call().result(3); self.offer(); self.tick(6)
        self.assertEqual(self.planner.calls,0)
        self.controller.end_call().result(3)
        self.controller.oath_session={'token':'fixture'}; self.offer(); self.tick(6)
        self.assertEqual(self.planner.calls,0)
        self.controller.oath_session=None
    def test_activity_invalidates_inflight_decision(self):
        self.enable(text=True); self.planner.release=threading.Event(); self.offer(); self.tick(6)
        self.assertTrue(self.planner.entered.wait(2)); self.calls.activity(); self.finish_decision()
        self.assertIsNone(self.controller.snapshot()['incoming'])
        self.assertEqual(self.controller.records(),[])
    def test_text_uses_completed_turn_and_stays_assistant_only(self):
        self.enable(text=True,calls=False)
        self.planner.result=TEXT
        query='昨天的项目现在有新进展'
        with patch.object(self.controller.lore,'context',return_value='有关的知识片段') as knowledge:
            self.controller.submit(query).result(3)
            self.assertTrue(self.controller.pipeline.wait_finished(3))
            self.tick(6); self.finish_decision()
        self.assertEqual(self.planner.context['available_actions'],['text'])
        self.assertEqual(self.planner.context['event'],{'source':'text','content':query})
        self.assertIn(query,[m['content'] for m in self.planner.context['conversation']])
        self.assertEqual(self.planner.context['knowledge'],'有关的知识片段')
        knowledge.assert_any_call(query,self.planner.context['conversation'])
        row=self.controller.records()[-1]
        self.assertEqual((row['source'],row['user'],row['assistant']),('proactive','',TEXT['message']))
        self.controller.executor.submit(lambda: None).result(3)
        self.assertEqual(self.voice.replayed,[TEXT['message']])
        self.assertEqual(self.controller.playback_record_id,row['id'])
        self.assertEqual(self.chat.history[-1],{'role':'assistant','content':TEXT['message']})
        self.controller.restore_contexts()
        self.assertEqual(self.chat.history[-1],{'role':'assistant','content':TEXT['message']})
        self.tick(60)
        self.assertEqual(self.planner.calls,1)
        self.assertEqual(self.controller.call.starts,[])
    def test_proactive_text_is_saved_without_speech_when_autoplay_is_off(self):
        self.enable(text=True,calls=False)
        self.controller.apply_preferences(replace(self.controller.preferences,autoplay=False)).result(3)
        self.planner.result=TEXT
        self.offer(); self.tick(6); self.finish_decision()
        self.controller.executor.submit(lambda: None).result(3)
        self.assertEqual(self.controller.records()[-1]['assistant'],TEXT['message'])
        self.assertEqual(self.voice.replayed,[])
        self.assertEqual(self.controller.call.starts,[])
    def test_disabled_channel_is_never_delivered(self):
        self.offer(); self.tick(6)
        self.assertIsNone(self.calls.future)
        self.enable(text=True,calls=False); self.offer(); self.tick(6); self.finish_decision()
        self.assertEqual(self.planner.context['available_actions'],['text'])
        self.assertIsNone(self.calls.snapshot())
        self.enable(); self.planner.result=TEXT; self.offer('今天的新项目完成了'); self.tick(6); self.finish_decision()
        self.assertEqual(self.planner.context['available_actions'],['call'])
        self.assertEqual(self.controller.records(),[])
    def test_affinity_admits_shorter_events_for_text_without_admitting_calls(self):
        self.enable(text=True)
        for score, message, stage in (
            (0,'图纸画完',None),
            (30,'图纸画完','familiar'),
            (30,'画完了',None),
            (70,'画完了','close'),
        ):
            with self.subTest(score=score,message=message):
                with self.controller.memory.lock,self.controller.memory.connection:
                    self.controller.memory.connection.execute(
                        "UPDATE profile SET affinity=?,stage='acquaintance'",(score,))
                self.offer(message); self.tick(6)
                if stage is None:
                    self.assertIsNone(self.calls.future)
                else:
                    self.assertIsNotNone(self.calls.future)
                    self.finish_decision()
                    self.assertEqual(self.planner.context['relationship_stage'],stage)
                    self.assertEqual(self.planner.context['available_actions'],['text'])
                    self.assertIsNone(self.calls.snapshot())
        self.offer(); self.tick(6); self.finish_decision()
        self.assertEqual(self.planner.context['available_actions'],['text','call'])
        self.assertIsNotNone(self.calls.snapshot())
    def test_voice_readiness_does_not_block_text(self):
        self.enable(text=True); self.planner.result=TEXT; self.offer()
        with patch.object(self.controller,'voice_load_state',return_value={'voice_ready':False}):
            self.tick(6); self.finish_decision()
        self.assertEqual(self.planner.context['available_actions'],['text'])
        self.assertEqual(self.controller.records()[-1]['assistant'],TEXT['message'])
    def test_model_failure_surfaces_reason_once(self):
        self.enable(); self.offer()
        with patch.object(self.planner,'decide',side_effect=ChatError('模型暂不可用')) as decide:
            self.tick(6)
            with self.assertRaises(ChatError): self.calls.future.result(3)
            self.clock.advance(1)
            events=self.controller.poll()
            self.assertIn({'kind':'incoming_decision_failed','message':'模型暂不可用'},events)
            self.tick(30)
            self.assertEqual(decide.call_count,1)
    def test_disabling_text_cancels_inflight_result(self):
        self.enable(text=True); self.planner.result=TEXT; self.planner.release=threading.Event()
        self.offer(); self.tick(6); self.assertTrue(self.planner.entered.wait(2))
        self.enable(text=False)
        self.finish_decision()
        self.assertEqual(self.controller.records(),[])
        self.assertIsNone(self.calls.snapshot())
    def test_manual_memory_is_a_trigger(self):
        self.enable(text=True,calls=False); self.planner.result={'action':'wait'}
        message='我喜欢在周末画建筑速写'
        self.controller.save_memory(message).result(3)
        self.tick(6); self.finish_decision()
        self.assertEqual(self.planner.context['event'],{'source':'memory','content':message})
        self.assertIn(message,self.planner.context['memory'])
    def test_voice_topic_is_considered_after_hangup(self):
        self.enable(); self.controller.start_call().result(3)
        self.controller.pipeline.submit('昨天的项目现在有新进展',input_metrics={})
        self.assertTrue(self.controller.pipeline.wait_finished(3))
        self.tick(6); self.assertEqual(self.planner.calls,0)
        self.controller.end_call().result(3)
        self.tick(6); self.finish_decision()
        self.assertEqual(self.planner.context['event']['source'],'voice')
        self.assertIsNotNone(self.calls.snapshot())
    def test_sleep_resume_discards_stale_event_and_misses_old_call(self):
        self.enable(); self.offer(); self.tick(1000); self.assertEqual(self.planner.calls,0)
        self.ring(); self.tick(1000)
        self.assertIsNone(self.controller.snapshot()['incoming']); self.assertEqual(self.calls.store.recent()[0]['state'],'missed')
    def test_accept_opening_is_assistant_only_and_voice_continues(self):
        identifier=self.ring(); score=self.controller.memory.profile()['affinity']
        self.assertTrue(self.controller.answer_call(identifier).result(3))
        self.assertTrue(self.controller.pipeline.wait_finished(3))
        rows=self.controller.records(); self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['source'],'incoming'); self.assertEqual(rows[0]['user'],'')
        self.assertEqual(rows[0]['state'],'completed'); self.assertEqual(self.chat.calls,0)
        self.assertEqual([m['role'] for m in self.controller.store.context('qwen')],['assistant'])
        self.assertEqual(self.chat.history[-1]['role'],'assistant')
        self.assertTrue(self.controller.call.active); self.assertEqual(len(self.voice.replayed),1)
        self.assertEqual(self.controller.memory.profile()['affinity'],score)
        self.controller.pipeline.submit('我在听',input_metrics={}); self.assertTrue(self.controller.pipeline.wait_finished(3))
        self.assertEqual(self.controller.records()[-1]['user'],'我在听')
        self.controller.end_call().result(3); self.assertFalse(self.controller.call.active)
        self.assertEqual(self.calls.store.find(identifier)['state'],'ended')
    def test_opening_context_retained_with_subsequent_pair(self):
        identifier=self.ring(); self.controller.answer_call(identifier).result(3); self.controller.pipeline.wait_finished(3)
        opening=self.controller.records()[0]['assistant']
        self.controller.pipeline.submit('你好',input_metrics={}); self.controller.pipeline.wait_finished(3)
        messages,omitted=self.chat._messages('继续')
        self.assertEqual(messages[1],{'role':'assistant','content':opening}); self.assertEqual(omitted,0)
    def test_decline_and_timeout_leave_mic_history_and_affinity_untouched(self):
        before=self.controller.memory.profile()['affinity']; a=self.ring()
        self.controller.decline_call(a).result(3)
        self.assertEqual(self.calls.store.find(a)['state'],'declined')
        b=self.ring(); self.tick(46); self.assertEqual(self.calls.store.find(b)['state'],'missed')
        self.assertEqual(self.controller.call.starts,[]); self.assertEqual(self.controller.records(),[])
        self.assertEqual(self.controller.memory.profile()['affinity'],before)
    def test_expired_answer_is_rejected(self):
        a=self.ring(); self.clock.advance(46); self.assertIsNone(self.controller.answer_call(a).result(3))
        self.assertEqual(self.controller.call.starts,[])
    def test_snooze_redelivers_once_and_survives_restart(self):
        a=self.ring(); self.controller.decline_call(a,later=True).result(3)
        self.assertEqual(self.calls.store.pending()[0]['id'],a)
        self.clock.advance(1800); self.calls.last_tick=self.clock()-1; self.controller.poll()
        self.assertEqual(self.calls.snapshot()['id'],a)
        self.controller.decline_call(a,later=True).result(3)
        self.assertEqual(self.calls.store.find(a)['state'],'declined'); self.assertEqual(self.calls.store.pending(),[])
    def test_disable_withdraws_incoming_without_stopping_ongoing_chat(self):
        self.enable(); a=self.ring(); self.enable(calls=False)
        self.assertIsNone(self.calls.snapshot()); self.assertEqual(self.calls.store.find(a)['state'],'withdrawn')
        self.controller.start_call().result(3)
        self.controller.apply_preferences(replace(self.controller.preferences,proactive_calls=True)).result(3)
        self.assertTrue(self.controller.call.active)
    def test_capture_failure_does_not_leave_connected_state(self):
        a=self.ring()
        with patch.object(self.controller.call,'start',side_effect=RuntimeError('麦克风不可用')):
            self.assertIsNone(self.controller.answer_call(a).result(3))
        self.assertFalse(self.controller.snapshot()['call_requested']); self.assertEqual(self.calls.store.find(a)['state'],'failed')
        self.assertEqual(self.voice.replayed,[])
    def test_async_capture_failure_marks_call_failed_and_stops_audio(self):
        a=self.ring(); self.controller.answer_call(a).result(3); self.controller.pipeline.wait_finished(3)
        with patch.object(self.controller.call,'timings',return_value={'state':'off','error':'device missing','mode':'speakers'}):
            self.controller.poll(); self.controller.executor.submit(lambda:None).result(3)
        self.assertFalse(self.controller.call_requested)
        self.assertEqual(self.calls.store.find(a)['state'],'failed')
    def test_can_disable_proactive_calls_without_valid_model_key(self):
        with patch.object(self.router,'validate',side_effect=RuntimeError('missing key')):
            self.controller.apply_preferences(replace(self.controller.preferences,proactive_calls=False)).result(3)
        self.assertFalse(self.controller.preferences.proactive_calls)
    def test_opening_synthesis_failure_ends_capture_and_marks_call_failed(self):
        a=self.ring(); job=SpeechJob(); job.error='synthesis failed'; job.finished.set()
        with patch.object(self.voice,'speak',return_value=job):
            self.controller.answer_call(a).result(3); self.controller.pipeline.wait_finished(3)
            self.controller.poll(); self.controller.executor.submit(lambda:None).result(3)
        self.assertFalse(self.controller.call.active); self.assertEqual(self.calls.store.find(a)['state'],'failed')
        self.assertEqual(self.controller.store.context('qwen'),[])
    def test_opening_is_cancelled_on_hangup_and_does_not_commit_context(self):
        a=self.ring(); job=SpeechJob()
        def speak(text): self.voice.jobs.append(job); return job
        with patch.object(self.voice,'speak',side_effect=speak):
            self.controller.answer_call(a).result(3)
            deadline=time.monotonic()+2
            while not self.voice.jobs and time.monotonic()<deadline: time.sleep(.01)
            self.controller.end_call().result(3); self.controller.pipeline.wait_finished(3)
        self.assertEqual(self.controller.records()[-1]['state'],'cancelled')
        self.assertEqual(self.controller.store.context('qwen'),[])
    def test_rejects_duplicate_accept(self):
        a=self.ring(); self.controller.answer_call(a).result(3)
        self.assertIsNone(self.controller.answer_call(a).result(3)); self.assertEqual(len(self.controller.call.starts),1)
    def test_declined_topic_cannot_switch_to_text(self):
        self.enable(text=True); self.offer(); self.tick(6); self.finish_decision()
        a=self.calls.snapshot()['id']
        self.controller.decline_call(a).result(3)
        self.planner.result=TEXT; self.offer('昨天的项目又推进了一点'); self.tick(6); self.finish_decision()
        self.assertIn(PROPOSAL['topic'],self.planner.context['contacted_topics'])
        self.assertEqual(self.planner.calls,2); self.assertEqual(self.controller.records(),[])
    def test_clear_history_removes_call_content_and_pending_reminders(self):
        a=self.ring(); self.controller.decline_call(a,later=True).result(3)
        self.controller.clear_history().result(3)
        self.assertEqual(self.calls.store.recent(),[]); self.assertEqual(self.calls.store.pending(),[])


class IncomingUiTests(IncomingBase):
    # Only the explicitly defined UI cases; run controller cases once above.
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
    def setUp(self):
        super().setUp()
        self.window=ClientWindow(self.controller,ROOT); self.window.show(); self.app.processEvents()
        self.wait_for(lambda:self.window.overlay.phase=='shown')
    def tearDown(self):
        if self.window.tray: self.window.tray.hide()
        super().tearDown(); self.window.close(); self.app.processEvents()

    def wait_for(self,predicate):
        deadline=time.monotonic()+3
        while not predicate() and time.monotonic()<deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(),'UI state did not settle')
        self.app.processEvents()

    def click(self, widget):
        ancestor=widget.parentWidget()
        while ancestor:
            if isinstance(ancestor,QScrollArea): ancestor.ensureWidgetVisible(widget,0,24); self.app.processEvents(); break
            ancestor=ancestor.parentWidget()
        center=widget.mapTo(self.window.overlay.surface,widget.rect().center())
        point=self.window.overlay.mapFromScene(center.x(),center.y())
        self.assertTrue(self.window.overlay.viewport().rect().contains(point))
        QTest.mouseClick(self.window.overlay.viewport(),Qt.MouseButton.LeftButton,pos=point); QTest.qWait(250)

    def test_mouse_click_keeps_inflight_text_and_keypress_cancels_pending_text(self):
        self.enable(text=True,calls=False)
        self.planner.result=TEXT
        self.planner.release=threading.Event()
        self.offer(); self.tick(6)
        self.assertTrue(self.planner.entered.wait(2))
        viewport=self.window.overlay.viewport()
        QTest.mouseClick(viewport,Qt.MouseButton.LeftButton,pos=viewport.rect().center())
        self.assertFalse(self.planner.release.is_set())
        self.planner.release.set()
        self.finish_decision()
        self.assertEqual(self.controller.records()[-1]['assistant'],TEXT['message'])
        self.offer('接下来准备做新的模型')
        self.assertIsNotNone(self.calls.pending_event)
        QTest.keyClick(viewport,Qt.Key.Key_A)
        self.assertIsNone(self.calls.pending_event)

    def test_proactive_settings_show_two_independent_switches_and_token_warning(self):
        self.window.settings(); QTest.qWait(350); pane=self.window.settings_pane
        self.assertEqual(pane.proactive_row.title.text(),'主动回应')
        self.click(pane.proactive_row); self.assertIs(pane.stack.currentWidget(),pane.call_page)
        from client_panels import Switch
        switches=pane.call_page.findChildren(Switch)
        self.assertEqual({w.accessibleName() for w in switches},{'主动文字','主动来电'})
        self.assertTrue(any('大量 Token' in label.text() for label in pane.call_page.findChildren(QLabel)))
        for width,height in ((960,602),(1280,802),(1600,1000)):
            self.window.resize(width,height); self.app.processEvents()
            self.assertEqual(self.window.overlay.surface.width(),328)
            self.assertEqual(pane.call_page.width(),328)
        self.window.resize(1280,802); self.app.processEvents()
        self.click(pane.proactive_text)
        self.wait_for(lambda:self.controller.preferences.proactive_text)
        self.assertFalse(self.controller.preferences.proactive_calls)
        self.click(pane.proactive_calls)
        self.wait_for(lambda:self.controller.preferences.proactive_calls)
        self.click(pane.proactive_text)
        self.wait_for(lambda:not self.controller.preferences.proactive_text)
        self.assertTrue(self.controller.preferences.proactive_calls)
        self.click(next(w for w in pane.call_page.findChildren(QPushButton) if w.text()=='‹ 设置'))
        self.assertIs(pane.stack.currentWidget(),pane.root)

    def test_chat_and_settings_share_clock_battery_signal_and_camera(self):
        QTest.qWait(300)
        with patch('client_scene.phone_clock',return_value='07:21'):
            for width,height in ((960,602),(1280,802),(1600,1000)):
                self.window.resize(width,height); self.app.processEvents()
                self.window.text(); self.wait_for(lambda:self.window.overlay.phase=='shown')
                self.window.overlay.surface.repaint(); self.app.processEvents()
                chat=self.window.grab().toImage()
                self.window.settings(); self.wait_for(lambda:self.window.overlay.phase=='shown')
                self.window.overlay.surface.repaint(); self.app.processEvents()
                settings=self.window.grab().toImage(); dpr=self.window.devicePixelRatioF()
                for x,y,w,h in ((835,106,70,26),(918,93,104,25),(1067,101,54,20)):
                    px=round((self.window.offset_x+x*self.window.scale)*dpr); py=round((self.window.offset_y+y*self.window.scale)*dpr)
                    pw=round(w*self.window.scale*dpr); ph=round(h*self.window.scale*dpr)
                    self.assertEqual(chat.copy(px,py,pw,ph),settings.copy(px,py,pw,ph))
                x=round((self.window.offset_x+1103*self.window.scale)*dpr); y=round((self.window.offset_y+110*self.window.scale)*dpr)
                self.assertEqual(chat.pixelColor(x,y).name(),'#252b35')
                def bottom(bounds):
                    bx,by,bw,bh=bounds
                    left=round((self.window.offset_x+bx*self.window.scale)*dpr)
                    top=round((self.window.offset_y+by*self.window.scale)*dpr)
                    right=left+round(bw*self.window.scale*dpr); end=top+round(bh*self.window.scale*dpr)
                    return max(py for py in range(top,end) if any(max(chat.pixelColor(px,py).getRgb()[:3])<120 for px in range(left,right)))
                self.assertLessEqual(abs(bottom((835,103,70,25))-bottom((1067,99,54,26))),1)
            self.window.resize(1280,802); self.window.text(); QTest.qWait(300)
            preview=Path(os.environ.get('RONOVA_PREVIEW_DIR',str(CODE/'verification'))); preview.mkdir(parents=True,exist_ok=True)
            self.window.grab().save(str(preview/'chat-status-bar.png'))

    def test_call_waits_for_voice_preparation_before_starting_capture(self):
        self.window.open_call()
        self.wait_for(lambda:self.window.screen=='call' and self.window.overlay.phase=='hidden')
        self.voice.engine='gpt'; self.voice.ready=threading.Event()
        self.voice.preparing=True
        self.controller.prepare_local_voice()
        self.window.refresh()
        self.assertFalse(self.window.voice_button.isEnabled())
        QTest.mouseClick(self.window.voice_button,Qt.MouseButton.LeftButton)
        self.assertFalse(self.controller.call.active)
        preview=Path(os.environ.get('RONOVA_PREVIEW_DIR',str(CODE/'verification')))
        preview.mkdir(parents=True,exist_ok=True)
        self.window.grab().save(str(preview/'voice-preparation-status.png'))
        with patch.object(self.window,'label') as label:
            self.window.grab()
            self.assertIn('正在准备语音',[call.args[1] for call in label.call_args_list])
        self.voice.preparing=False
        self.voice.ready.set()
        self.window.refresh()
        self.assertTrue(self.window.voice_button.isEnabled())
        self.controller.start_call().result(3)
        self.window.refresh()
        with patch.object(self.window,'label') as label:
            self.window.grab()
            self.assertIn('正在聆听',[call.args[1] for call in label.call_args_list])

    def test_voice_call_button_is_green_and_status_follows_chat(self):
        self.window.open_call(); QTest.qWait(300)
        with patch.object(self.window,'label',wraps=self.window.label) as label:
            self.window.grab()
        labels = {call.args[1] for call in label.call_args_list}
        self.assertIn('等待通话',labels)
        self.assertIn('--:--',labels)
        self.assertTrue(labels.isdisjoint(('语音通话','结束通话','文字聊天')))
        for size in ((960,602),(1280,802),(1600,1000)):
            self.window.resize(*size); self.app.processEvents(); image=self.window.grab().toImage(); dpr=self.window.devicePixelRatioF()
            x=round((self.window.offset_x+846*self.window.scale)*dpr); y=round((self.window.offset_y+690*self.window.scale)*dpr)
            self.assertEqual(image.pixelColor(x,y).name(),'#37aa7a')
        self.controller.start_call().result(3); self.window.refresh()
        self.assertEqual(self.window.settings_pane.profile_status.text(),'陪你聊天')
        self.controller.mute().result(3); self.window.refresh(); image=self.window.grab().toImage()
        self.assertEqual(image.pixelColor(x,y).name(),'#37aa7a')
        self.controller.end_call().result(3); self.window.refresh()
        self.assertNotEqual(self.window.settings_pane.profile_status.text(),'陪你聊天')
        self.window.resize(1280,802); self.app.processEvents()
        preview=Path(os.environ.get('RONOVA_PREVIEW_DIR',str(CODE/'verification'))); preview.mkdir(parents=True,exist_ok=True)
        self.window.grab().save(str(preview/'call-green-button.png'))
        self.assertFalse(any('若娜瓦声线' in label.text() for label in self.window.settings_pane.root.findChildren(QLabel)))
    def test_phone_incoming_resize_accept_decline_and_camera(self):
        self.ring(); self.window.refresh()
        self.wait_for(lambda:self.window.screen=='call' and self.window.overlay.phase=='hidden')
        self.assertEqual(self.window.screen,'call')
        self.assertEqual(self.window.voice_button.accessibleName(),'接听')
        self.assertEqual(self.window.end_button.accessibleName(),'拒接')
        self.assertFalse(self.controller.call.active)
        for size in ((960,602),(1280,802),(1600,1000)):
            self.window.resize(*size); self.app.processEvents()
            for b in (self.window.voice_button,self.window.end_button,self.window.text_button):
                self.assertTrue(b.isVisible()); self.assertTrue(self.window.rect().contains(b.geometry()))
            image=self.window.grab().toImage(); dpr=self.window.devicePixelRatioF()
            x=round((self.window.offset_x+970*self.window.scale)*dpr); y=round((self.window.offset_y+105*self.window.scale)*dpr)
            self.assertEqual(image.pixelColor(x,y).name(),'#080b0d')
        preview=Path(os.environ.get('RONOVA_PREVIEW_DIR',str(CODE/'verification'))); preview.mkdir(parents=True,exist_ok=True)
        self.window.resize(1280,802); self.app.processEvents(); self.window.grab().save(str(preview/'incoming-call.png'))
        QTest.mouseClick(self.window.voice_button,Qt.MouseButton.LeftButton); QTest.qWait(200)
        self.assertTrue(self.controller.pipeline.wait_finished(3)); self.assertTrue(self.controller.call.active)
        self.window.refresh(); QTest.mouseClick(self.window.end_button,Qt.MouseButton.LeftButton); QTest.qWait(100)
        self.assertFalse(self.controller.call.active)
        self.ring(); self.window.refresh(); QTest.mouseClick(self.window.end_button,Qt.MouseButton.LeftButton); QTest.qWait(100)
        self.assertEqual(self.calls.store.recent()[0]['state'],'declined')
    def test_opening_chat_has_only_assistant_bubble(self):
        a=self.ring(); self.controller.answer_call(a).result(3); self.controller.pipeline.wait_finished(3)
        self.window.text(); QTest.qWait(400); self.window.refresh()
        labels=self.window.text_pane.history.findChildren(QLabel)
        self.assertEqual(sum(label.objectName()=='userBubble' for label in labels),0)
        self.assertEqual(sum(label.objectName()=='assistantBubble' for label in labels),1)
    def test_tray_close_preserves_controller_and_explicit_quit_stops(self):
        self.window.tray=type('Tray',(),{'hide':lambda self:None,'setToolTip':lambda self,text:None})()
        self.window.close(); self.app.processEvents()
        self.assertFalse(self.window.isVisible()); self.assertFalse(self.controller.closing)
        self.window.quit_client(); self.controller.close_future.result(5)
        self.assertTrue(self.controller.closed)


if __name__=='__main__':
    suite=unittest.TestSuite()
    for cls in (PolicyTests,IncomingTests): suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
    for name in IncomingUiTests.__dict__:
        if name.startswith('test_'): suite.addTest(IncomingUiTests(name))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    import json
    directory=Path(os.environ.get('RONOVA_PREVIEW_DIR',str(CODE/'verification'))); directory.mkdir(parents=True,exist_ok=True)
    report={'tests':result.testsRun,'passed':result.wasSuccessful(),'cloud_requests':0,'physical_microphone_started':False,
            'failures':[(str(t),text) for t,text in result.failures], 'errors':[(str(t),text) for t,text in result.errors]}
    (directory/'incoming-tests.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    sys.exit(not result.wasSuccessful())
