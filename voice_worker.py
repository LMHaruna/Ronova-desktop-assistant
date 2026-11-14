"""Persistent local voice worker with automatic CUDA selection and CPU fallback."""
from pathlib import Path
import gc
import json
import os
import queue
import re
import sys
import threading
import time
import wave

ROOT = Path(__file__).resolve().parent
TRANSPORT = sys.stdout
sys.stdout = sys.stderr
LOCK = threading.Lock()
SYNTHESIZE_ON_MAIN_THREAD = True


def emit(event, job=None, **values):
    with LOCK:
        TRANSPORT.write(json.dumps({"event": event, "job": job, **values}, ensure_ascii=False) + "\n")
        TRANSPORT.flush()


# IndexTTS owns model loading; this module provides its cancellable job protocol.
from index_voice_backend import initialize


def parameters(reference, text):
    return {"text": text, "ref_audio_path": str(ROOT / reference["path"]),
            "reference_max_seconds": max(15, float(reference.get("seconds", 15)))}


def main():
    # Preserve the Windows fix: native libraries initialize on the main thread
    # BEFORE any other thread starts reading stdin or using these libraries.
    first = sys.stdin.readline()
    if not first:
        return
    first_task = json.loads(first)
    if first_task.get("action") != "prepare":
        return
    model = None
    prepare_started = time.perf_counter()
    try:
        # Failed CUDA model loads or warmups (including low VRAM) retry once on
        # CPU. Run this on the main thread before starting protocol threads.
        for force_cpu in (False, True):
            try:
                model, reference, initialize_seconds, acceleration = initialize(force_cpu)
                import numpy as np
                warm_started = time.perf_counter()
                nonzero = False
                for sr, data in model.run(parameters(reference, "凡人，坐下。")):
                    nonzero = nonzero or bool(np.any(data))
                if not nonzero:
                    raise RuntimeError("Warmup did not produce speech")
                break
            except Exception as error:
                # Retry once using CPU kernels from the same CUDA package.
                import torch
                if force_cpu or torch.version.cuda is None or os.environ.get('RONOVA_VOICE_DEVICE') == 'cpu':
                    raise
                fallback_reason = type(error).__name__
                model = None
                gc.collect()
                try: torch.cuda.empty_cache()
                except Exception: pass
                emit('status', message='显卡语音准备失败，正在切换 CPU')
        if force_cpu:
            acceleration['fallback_reason'] = fallback_reason
        if acceleration['device'] == 'cuda':
            import torch
            acceleration['gpu_memory_peak_mb'] = torch.cuda.max_memory_allocated()/1024**2
        speaker_cache = model.reference_speaker_cache
        emit("ready", initialize_seconds=initialize_seconds,
             warmup_seconds=time.perf_counter() - warm_started,
             prepare_seconds=time.perf_counter() - prepare_started,
             **acceleration,
             model_version=model.configs.version, vits_weights_path=model.configs.vits_weights_path,
             engine="gpt", reference_id=reference["id"], reference_audio=reference["path"],
             reference_warmup=True, reference_speaker_cache=getattr(model, 'sv_model', None) is not None,
             speaker_cache_hits=speaker_cache.hits, speaker_cache_misses=speaker_cache.misses)
    except Exception as exc:
        # Startup tracebacks contain local model paths, never chat or keys.
        import traceback
        traceback.print_exc(file=sys.stderr)
        emit("boot_error", message=f"语音准备失败：{type(exc).__name__}")
        return

    tasks = queue.Queue(maxsize=128)
    states = {}
    state_lock = threading.Lock()
    current = [None]

    def run():
        while True:
            task = tasks.get()
            if task is None:
                return
            job = task["job"]
            with state_lock:
                state = states.get(job)
                if state is None:
                    continue
                current[0] = job
            if task["action"] == "finish":
                if not state["cancelled"] and not state["nonzero"]:
                    emit("error", job, message="本轮没有可播放的语音")
                else:
                    emit("done", job, cancelled=state["cancelled"], fragments=state["count"],
                         device=acceleration['device'], is_half=acceleration['is_half'],
                         generation_seconds=state["generation_seconds"], load_seconds=0.0,
                         first_fragment_seconds=state["first_fragment_seconds"], segments=state["segments"],
                         queue_wait_seconds=state["queue_wait_seconds"],
                         speaker_cache_hits=state["speaker_cache_hits"],
                         speaker_cache_misses=state["speaker_cache_misses"])
                with state_lock:
                    states.pop(job, None)
                    current[0] = None
                continue
            if not state["cancelled"] and not state["failed"]:
                started = time.perf_counter()
                state["queue_wait_seconds"] += max(0.0, started - task["queued"])
                cache_hits, cache_misses = speaker_cache.hits, speaker_cache.misses
                if hasattr(model, 'set_cancel_check'):
                    model.set_cancel_check(lambda: state['cancelled'])
                generator = model.run(parameters(reference, task["text"]))
                try:
                    for sr, data in generator:
                        if state["cancelled"]:
                            break
                        pcm = np.asarray(data, dtype=np.int16)
                        if not pcm.size:
                            continue
                        audible = bool(np.any(pcm))
                        state["nonzero"] |= audible
                        if audible and state["first_fragment_seconds"] is None:
                            state["first_fragment_seconds"] = time.perf_counter() - state["created"]
                        path = ROOT / "runtime/chat-voice-tmp" / job / f"{state['count']:04d}.wav"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with wave.open(str(path), "wb") as wav:
                            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sr)
                            wav.writeframes(pcm.tobytes())
                        emit("fragment", job, index=state["count"], path=str(path), sample_rate=sr,
                             audio_seconds=pcm.size / sr, silent=not audible)
                        state["count"] += 1
                    state["segments"] += 1
                except Exception as exc:
                    if not state["cancelled"]:
                        state["failed"] = True
                        emit("error", job, terminal=False, message=f"本地合成失败：{type(exc).__name__}")
                finally:
                    generator.close()
                    state["generation_seconds"] += time.perf_counter() - started
                    state["speaker_cache_hits"] += speaker_cache.hits - cache_hits
                    state["speaker_cache_misses"] += speaker_cache.misses - cache_misses
            with state_lock:
                current[0] = None

    def read_controls():
        for line in sys.stdin:
            try:
                task = json.loads(line)
                action = task["action"]
                if action == "close":
                    break
                if action == "prepare":
                    emit("ready", initialize_seconds=0.0, warmup_seconds=0.0)
                    continue
                job = task["job"]
                if not re.fullmatch(r"[0-9a-f]{32}", job):
                    continue
                with state_lock:
                    if action == "begin":
                        states[job] = {"created": time.perf_counter(),
                                       "cancelled": False, "failed": False, "nonzero": False, "count": 0,
                                       "segments": 0, "generation_seconds": 0.0, "first_fragment_seconds": None,
                                       "queue_wait_seconds": 0.0, "speaker_cache_hits": 0,
                                       "speaker_cache_misses": 0}
                    elif action == "cancel" and job in states:
                        states[job]["cancelled"] = True
                        if current[0] == job:
                            model.stop()
                    elif action in ("append", "finish") and job in states:
                        if action == "append" and (not isinstance(task.get("text"), str) or not task["text"].strip()):
                            continue
                        task["queued"] = time.perf_counter()
                        # Do not block the stdin reader: it also carries urgent cancellation.
                        try:
                            tasks.put_nowait(task)
                        except queue.Full:
                            states[job]["cancelled"] = True
                            if current[0] == job:
                                model.stop()
                            emit("error", job, terminal=False, message="语音队列已满，文字保留")
                            if action == "finish":
                                # Acknowledgement is essential for safe cleanup. Defer it
                                # instead of blocking cancellation on the reader thread.
                                threading.Thread(target=tasks.put, args=(task,), daemon=True).start()
            except (ValueError, KeyError, TypeError):
                continue

        # Release the protocol thread before native runtimes shut down on Windows.
        with state_lock:
            for state in states.values():
                state["cancelled"] = True
            if current[0] is not None:
                model.stop()
        if SYNTHESIZE_ON_MAIN_THREAD:
            tasks.put(None)  # Reader has stopped; queued cancelled tasks drain promptly.
        else:
            try:
                tasks.put_nowait(None)
            except queue.Full:
                pass  # The owner retains a bounded forced-exit fallback.

    if SYNTHESIZE_ON_MAIN_THREAD:
        reader = threading.Thread(target=read_controls, daemon=True)
        reader.start()
        run()
        reader.join(timeout=1)
    else:
        runner = threading.Thread(target=run, daemon=True)
        runner.start()
        read_controls()
        runner.join(timeout=5)


if __name__ == "__main__":
    main()
