"""Compare native speech pauses and starvation between Index synthesis clauses."""
from pathlib import Path
import argparse
import contextlib
import hashlib
import json
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEXTS = {
    'continuous': '我会先把今天发生的事情仔细讲给你听然后再陪你一起看看接下来应该怎么安排',
    'short-clauses': '你先坐下。别着急。我在听。慢慢说。今天发生了什么事情，可以从头告诉我，我们一起想办法。',
}

def save(path, data, rate):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(rate)
        output.writeframes(data.tobytes())

def pauses(data, rate):
    import numpy as np
    window = round(rate*.01)
    values = data.astype('float64')/32768
    quiet = [np.sqrt(np.mean(values[i:i+window]**2)) < 10**(-42/20)
             for i in range(0,len(values),window)]
    result, start = [], None
    for i, silent in enumerate(quiet+[False]):
        if silent and start is None: start = i*.01
        elif not silent and start is not None:
            end = min(i*.01,len(data)/rate)
            if end-start >= .18:
                result.append({'start':start,'end':end,'seconds':end-start})
            start = None
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args(); out = args.directory.resolve(); out.mkdir(parents=True,exist_ok=True)
    from index_voice_backend import initialize
    from reply_pipeline import SentenceBuffer
    import numpy as np
    report = {'runs':[],'cloud_requests':0,'physical_playback':False,'microphone_started':False}
    with (out/'synthesis.log').open('w',encoding='utf-8') as log, \
            contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        model, ref, load_seconds, acceleration = initialize()
        def parameters(text):
            return {'text':text,'ref_audio_path':str(ROOT/ref['path']),
                    'reference_max_seconds':max(15,float(ref.get('seconds',15)))}
        for _ in model.run(parameters('凡人，坐下。')): pass
        report.update(reference=ref,load_seconds=load_seconds,acceleration=acceleration)
        for name, policy in [('continuous','whole'),('short-clauses','legacy'),('short-clauses','merged')]:
            text = TEXTS[name]; clauses = []
            if policy == 'whole': clauses = [text]
            else:
                buffer = (SentenceBuffer.for_engine(clauses.append,'indextts-2.5')
                          if policy=='merged' else SentenceBuffer(clauses.append))
                for char in text: buffer.feed(char)
                buffer.finish()
            chunks, timeline, fragments, playhead = [], [], [], None
            started = time.perf_counter()
            for clause in clauses:
                for rate, audio in model.run(parameters(clause)):
                    arrival = time.perf_counter()-started; seconds = len(audio)/rate
                    gap = max(0,arrival-playhead) if playhead is not None else 0
                    if gap:
                        timeline.append(np.zeros(round(gap*rate),dtype=np.int16))
                    chunks.append(audio);timeline.append(audio)
                    fragments.append({'text':clause,'characters':sum('\u4e00'<=c<='\u9fff' for c in clause),
                                      'arrival_seconds':arrival,'audio_seconds':seconds,'supply_gap_seconds':gap})
                    playhead = max(arrival,playhead or 0)+seconds
            if not chunks: raise RuntimeError('No Index speech')
            stem=name+'--'+policy; audio=np.concatenate(chunks)
            save(out/(stem+'-source.wav'),audio,rate)
            save(out/(stem+'-playback.wav'),np.concatenate(timeline),rate)
            run={'case':name,'policy':policy,'text':text,'clauses':clauses,
                 'fragments':fragments,'audio_seconds':len(audio)/rate,
                 'generation_seconds':time.perf_counter()-started,
                 'source_wav':stem+'-source.wav','playback_wav':stem+'-playback.wav',
                 'encoded_pauses':pauses(audio,rate),
                 'longest_supply_gap_seconds':max(f['supply_gap_seconds'] for f in fragments),
                 'total_supply_gap_seconds':sum(f['supply_gap_seconds'] for f in fragments),
                 'sha256':hashlib.sha256((out/(stem+'-source.wav')).read_bytes()).hexdigest()}
            report['runs'].append(run)
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print('COMPLETED',name,policy,run['longest_supply_gap_seconds'],flush=True)
    print(json.dumps([{'case':r['case'],'policy':r['policy'],'clauses':r['clauses'],
                      'longest_supply_gap_seconds':r['longest_supply_gap_seconds'],
                      'total_supply_gap_seconds':r['total_supply_gap_seconds']} for r in report['runs']],ensure_ascii=False),flush=True)

if __name__=='__main__': main()
