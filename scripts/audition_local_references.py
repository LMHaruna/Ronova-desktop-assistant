"""Generate one matching readout for each explicitly supplied reference and engine."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEXT = '你来了，凡人。今天发生了什么，说给我听听。别急，慢慢说，我在听。你倒是比我想象中更有耐心。说完以后，就早点休息吧。'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=('indextts-2.5',), required=True)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    references = json.loads((directory/'references.json').read_text(encoding='utf-8'))['references']
    from index_voice_backend import initialize
    model, _, load_seconds, acceleration = initialize()
    import numpy as np
    report = {'engine': args.engine, 'load_seconds': load_seconds, 'acceleration': acceleration,
              'text': TEXT, 'runs': [], 'reference_processing': {
                  'input': 'complete requested reference; mono 32 kHz, peak normalized to -3 dBFS',
                  'index_max_seconds': 'explicit input duration; default 15 second crop overridden for audition'}}
    for reference in references:
        target = directory/(args.engine+'--'+reference['id']+'.wav')
        if target.exists():
            raise FileExistsError('Preserving existing audition: '+str(target))
        parameters = {'text': TEXT, 'ref_audio_path': str(ROOT/reference['path']),
                      'reference_max_seconds': max(15, reference['seconds'])}
        started = time.perf_counter()
        chunks = []
        sample_rate = None
        for rate, data in model.run(parameters):
            if sample_rate is not None and sample_rate != rate:
                raise RuntimeError('Mixed sample rates')
            sample_rate = rate
            chunks.append(np.asarray(data, dtype=np.int16).reshape(-1))
            print('FRAGMENT', args.engine, reference['id'], len(chunks), len(data)/rate, flush=True)
        if not chunks or not any(np.any(chunk) for chunk in chunks):
            raise RuntimeError('No speech generated')
        audio = np.concatenate(chunks)
        with wave.open(str(target), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(audio.tobytes())
        run = {'reference_id': reference['id'], 'reference_seconds': reference['seconds'],
               'reference_path': reference['path'], 'prompt_text': reference['text'],
               'wav': target.name, 'sample_rate': sample_rate,
               'audio_seconds': len(audio)/sample_rate, 'generation_seconds': time.perf_counter()-started,
               'peak_dbfs': float(20*np.log10(max(float(np.max(np.abs(audio.astype(np.float64))))/32768, 1e-12))),
               'clipped_percent': float(np.mean(np.abs(audio.astype(np.int32)) >= 32767)*100),
               'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'parameters': parameters}
        report['runs'].append(run)
        (directory/(args.engine+'-report.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('SAVED', json.dumps(run, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
