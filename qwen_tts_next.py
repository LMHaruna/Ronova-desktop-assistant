"""Synthesize text replies with the reference used for Ronova's realtime Plus voice."""
import asyncio
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
import threading
from urllib.parse import urlsplit
import wave

import aiohttp

from chat_client import ChatError
from qwen_voice_clone import EnrollmentError, reference_data
from realtime_voice import RealtimeVoice

TTS_NEXT = 'qwen-audio-3.1-tts-next'
TTS_NEXT_URL = 'https://maas.qianwenaiapi.com/api/v1/services/audio/tts/SpeechSynthesizer'
RONOVA_REFERENCE = Path('voice-reference/prepared/qwen-voice-reference.wav')
MAX_WAV_BYTES = 6 * 1024 * 1024


@dataclass(frozen=True)
class TTSNextConfig:
    api_key: str = field(repr=False)
    reference: Path

    def validate(self):
        if not self.api_key or any(c.isspace() for c in self.api_key):
            raise ChatError('请填写 TTS Next 的 API Key')


def check_response(response, stage):
    if response.status == 200:
        return
    reason = {401:'请检查 API Key', 403:'请检查 API Key 的模型权限',
              404:'请检查模型是否已开通', 429:'请稍后重试'}.get(response.status)
    raise ChatError(f'TTS Next {stage}失败（HTTP {response.status}）' + (f'，{reason}' if reason else ''))


async def synthesize(session, config, reference, text):
    headers = {'Authorization': 'Bearer ' + config.api_key}
    payload = {'model': TTS_NEXT, 'input': {
        'text_prompt': f'@voice1 用中文原样朗读：“{text}”',
        'references': [{'audio_data': reference}],
        'format': 'wav', 'sample_rate': 24000, 'channels': 1}}
    async with session.post(TTS_NEXT_URL, headers=headers, json=payload, allow_redirects=False) as response:
        check_response(response, '合成')
        output = (await response.json())['output']
        if output['finish_reason'] != 'stop':
            raise ChatError('TTS Next 合成未完整结束')
        url = output['audio']['url']
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ChatError('TTS Next 返回的音频地址无效')
    data = bytearray()
    async with session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=60)) as response:
        check_response(response, '下载')
        async for chunk in response.content.iter_chunked(64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_WAV_BYTES:
                raise ChatError('TTS Next 音频超过长度上限')
    with wave.open(BytesIO(data), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 24000):
            raise ChatError('TTS Next 音频格式须为 24kHz 单声道 16 位 WAV')
        frames = audio.getnframes()
        pcm = audio.readframes(frames)
        if not frames or len(pcm) != frames * 2:
            raise ChatError('TTS Next 音频数据不完整')
        return pcm


class TTSNextVoice(RealtimeVoice):
    engine = tts_engine = TTS_NEXT
    whole_reply = True

    def prepare(self):
        if self.ready.is_set(): return self.ready
        self.config.validate()
        if not self.config.reference.is_file():
            raise ChatError('找不到实时 Plus 使用的若娜瓦参考录音')
        self.ready.set()
        return self.ready

    def begin(self):
        job = super().begin()
        job.text_parts = []
        job.config = self.config
        job.metrics.update(engine=self.engine, tts_engine=self.tts_engine, playback_strategy='cloud_wav')
        return job

    def append(self, job, text):
        if not job.input_closed and not job.cancelled.is_set():
            job.text_parts.append(text)

    def speak(self, text):
        job = self.begin()
        self.append(job, text)
        self.end(job)
        return job

    def end(self, job):
        if job.input_closed:
            return
        job.input_closed = True
        text = ''.join(job.text_parts).strip()
        if not text or job.cancelled.is_set():
            self._complete_input(job)
            return
        job.synth_thread = threading.Thread(target=self._synthesize, args=(job, text), daemon=True)
        job.synth_thread.start()

    def _synthesize(self, job, text):
        try:
            reference = reference_data(job.config.reference)
            asyncio.run(self._render(job, text, reference))
        except Exception as exc:
            if not job.cancelled.is_set():
                job.error = str(exc) if isinstance(exc, (ChatError, EnrollmentError)) else 'TTS Next 语音合成或下载失败'
                self.status(job.error)
        finally:
            self._complete_input(job)

    async def _render(self, job, text, reference):
        timeout = aiohttp.ClientTimeout(total=300, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while text and not job.cancelled.is_set():
                end = len(text)
                if end > 2800:
                    punctuation = max(text.rfind(mark, 1400, 2800) for mark in '。！？\n')
                    end = punctuation + 1 if punctuation >= 0 else 2800
                part, text = text[:end], text[end:]
                task = asyncio.create_task(synthesize(session, job.config, reference, part))
                try:
                    while not task.done() and not job.cancelled.is_set():
                        await asyncio.wait((task,), timeout=.05)
                    if not job.cancelled.is_set():
                        self._queue_pcm(job, task.result())
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
