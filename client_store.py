"""Local text history and non-secret desktop preferences."""
from pathlib import Path
from dataclasses import asdict, dataclass, fields
import json
import math
import sqlite3
import threading
import time
from client_models import ENGINES, VOICE_SETTINGS_ENGINES
from qwen_tts_next import TTS_NEXT


@dataclass
class Preferences:
    engine: str = 'qwen'
    gpt_tts_engine: str = 'indextts-2.5'
    deepseek_tts_engine: str = 'indextts-2.5'
    qwen_max_tts_engine: str = 'indextts-2.5'
    call_mode: str = 'speakers-aec'
    microphone: int | None = None
    speaker: int | None = None
    volume: float = .8
    autoplay: bool = True
    start_on_login: bool = True
    proactive_text: bool = False
    proactive_calls: bool = False
    desktop_pinned: bool = True
    font_scale: int = 100
    theme: str = 'light'

    def validate(self):
        if self.engine not in ENGINES: raise ValueError('大模型选项无效')
        for engine in VOICE_SETTINGS_ENGINES:
            if getattr(self,engine+'_tts_engine') not in ('indextts-2.5', TTS_NEXT):
                raise ValueError('语音合成引擎无效')
        # Migrate the retired desktop choice; CLI voice modes remain independent.
        if self.call_mode == 'speakers': self.call_mode = 'speakers-aec'
        if self.call_mode not in ('speakers-aec', 'headphones'): raise ValueError('通话模式无效')
        for value in (self.microphone, self.speaker):
            if value is not None and (type(value) is not int or value < 0): raise ValueError('设备编号无效')
        if type(self.volume) not in (int,float) or not math.isfinite(self.volume) or not 0 <= self.volume <= 1:
            raise ValueError('音量无效')
        if type(self.autoplay) is not bool: raise ValueError('自动播放配置无效')
        if type(self.start_on_login) is not bool: raise ValueError('开机自动启动配置无效')
        if type(self.desktop_pinned) is not bool: raise ValueError('桌面置顶配置无效')
        if type(self.font_scale) is not int or not 80 <= self.font_scale <= 130: raise ValueError('文字字号配置无效')
        if self.theme not in ('light', 'dark'): raise ValueError('界面主题配置无效')
        if type(self.proactive_text) is not bool or type(self.proactive_calls) is not bool:
            raise ValueError('主动回应开关无效')
        return self

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.exists(): return cls()
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if 'proactive_text' not in value:
            # The retired timer enabled calls by default. The new mode requires opt-in.
            value.pop('proactive_calls',None)
        for name in ('gpt_tts_engine', 'deepseek_tts_engine'):
            if value.get(name) in ('gpt-sovitsv2proplus', 'gpt-sovitsv5turbo'):
                value[name] = 'indextts-2.5'
        allowed = {field.name for field in fields(cls)}
        return cls(**{name: value[name] for name in allowed if name in value}).validate()

    def save(self, path):
        self.validate()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name+'.tmp')
        temporary.write_text(json.dumps(asdict(self),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(path)


class HistoryStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA secure_delete=ON')
        self.connection.execute('''CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY, generation TEXT NOT NULL, backend TEXT NOT NULL,
            source TEXT NOT NULL, user TEXT NOT NULL, assistant TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL, updated REAL NOT NULL)''')
        # A prior process cannot leave a record pretending to be in flight.
        self.connection.execute("UPDATE turns SET state='interrupted' WHERE state='pending'")
        self.connection.commit()

    def begin(self, generation, backend, text, source='text', retry_id=None):
        if backend not in ENGINES or (not text.strip() and source!='incoming'): raise ValueError('历史话轮无效')
        with self.lock, self.connection:
            if retry_id:
                existing = self.connection.execute('SELECT state FROM turns WHERE id=?',(retry_id,)).fetchone()
                if not existing or existing['state'] not in ('failed','interrupted','cancelled'):
                    raise ValueError('该话轮无需重试')
                self.connection.execute('''UPDATE turns SET generation=?, assistant='', state='pending',
                    error=NULL, attempts=attempts+1, updated=? WHERE id=?''',(generation,time.time(),retry_id))
                return retry_id
            self.connection.execute('''INSERT INTO turns(id,generation,backend,source,user,state,created,updated)
                VALUES(?,?,?,?,?,'pending',?,?)''',(generation,generation,backend,source,text,time.time(),time.time()))
            return generation

    def update(self, generation, reply, state='pending', error=None):
        if state not in ('pending','completed','failed','cancelled','interrupted'): raise ValueError('历史状态无效')
        with self.lock, self.connection:
            self.connection.execute('''UPDATE turns SET assistant=?, state=?, error=?, updated=?
                WHERE generation=? AND state='pending' ''',(reply,state,error,time.time(),generation))

    def recent(self, limit=100):
        with self.lock:
            rows = self.connection.execute('SELECT * FROM turns ORDER BY created DESC, rowid DESC LIMIT ?',
                                          (max(1,min(limit,500)),)).fetchall()
            return [dict(row) for row in reversed(rows)]

    def add_proactive_text(self, identifier, backend, message):
        now = time.time()
        with self.lock, self.connection:
            self.connection.execute("""INSERT INTO turns(id,generation,backend,source,user,assistant,state,created,updated)
                VALUES(?,?,?,'proactive','',?,'completed',?,?)""",(identifier,identifier,backend,message,now,now))

    def context(self, backend, limit=20, since=0):
        with self.lock:
            rows = self.connection.execute("SELECT user,assistant FROM turns WHERE backend=? AND state='completed' AND created>? ORDER BY created DESC, rowid DESC LIMIT ?",
                                           (backend,since,max(1,min(limit,20)))).fetchall()
            return [message for row in reversed(rows) for message in
                    ([{'role':'user','content':row['user']}] if row['user'] else [])+
                    [{'role':'assistant','content':row['assistant']}]]

    def clear(self):
        with self.lock:
            with self.connection: self.connection.execute('DELETE FROM turns')
            self.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.connection.execute('VACUUM')

    def close(self):
        with self.lock: self.connection.close()
