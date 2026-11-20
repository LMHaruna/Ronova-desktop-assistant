"""Stage the Windows desktop release with offline wheels and a file manifest."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

if __package__:
    from .build_desktop_launcher import LAUNCHER_NAME, build_launcher
else:
    from build_desktop_launcher import LAUNCHER_NAME, build_launcher

ROOT = Path(__file__).resolve().parents[1]
BUNDLED_PYTHON = 'runtime/python/cpython-3.11.17-windows-x86_64-none'
PYTHON_VENV_MODULE = ROOT / BUNDLED_PYTHON / 'Lib/venv'
PACKAGE_PATHS = (
    'README.md', 'THIRD_PARTY_NOTICES.md', 'VERSION', 'docs/screenshots',
    'config/character-knowledge.json', 'config/client-models.json',
    'config/index-voice.json', 'config/realtime-voice.example.json', 'config/voice-selection.json',
    'knowledge', 'licenses',
    'design/tokens.json', 'design/client-assets', 'design/client-audio', 'design/oath-video',
    'design/character-final/avatar-ronova-chibi.png',
    'design/character-final/avatar-ronova-chibi-working.png',
    'design/character-final/ronova-call.png',
    'design/character-final/relationship-portraits.json',
    'design/character-final/qwen-image-3.0/cleaned',
    'design/desktop-assistant-icons',
    'voice-reference/prepared/references.json', 'voice-reference/prepared/asr-near-end.wav',
    'voice-reference/prepared/asr-far-end-echo.wav', 'voice-reference/prepared/indextts-fallback.wav',
    'voice-reference/prepared/qwen-voice-reference.wav',
    'runtime/echo-tools', 'runtime/indextts-2.5',
    'runtime/models', 'runtime/prerequisites', 'runtime/presentmon', 'runtime/wheelhouse', 'runtime/wheelhouse-cuda',
    BUNDLED_PYTHON,
)
EXCLUDED_DIRS = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', '.pytest_cache',
                 '.codex', '.agents', '.aws', '.ssh', 'credentials'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.tmp', '.part', '.download-part', '.log',
                     '.pem', '.p12', '.pfx', '.sqlite3'}
EXCLUDED_PATHS = {
    ROOT / 'runtime/indextts-2.5/environment-lock.txt',
    ROOT / 'runtime/indextts-2.5/extras/import-manifest.json',
}


def is_link(path):
    return path.is_symlink() or bool(
        getattr(path.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def package_files():
    paths = set(ROOT.glob('*.py')) | set(ROOT.glob('*.cmd'))
    for pattern in ('*.py', '*.cmd', '*requirements*.txt'):
        paths.update((ROOT / 'scripts').glob(pattern))
    selection = json.loads((ROOT / 'config/voice-selection.json').read_text(encoding='utf-8'))
    reference_path = Path(selection['reference_audio'])
    reference = ROOT / reference_path
    if ('..' in reference_path.parts or reference_path.is_absolute()
            or not reference.is_relative_to(ROOT / 'voice-reference/prepared')):
        raise ValueError('The configured reference audio must be in voice-reference/prepared.')
    paths.add(reference)
    for name in PACKAGE_PATHS:
        path = ROOT / name
        if is_link(path):
            raise ValueError(f'Unexpected link: {name}')
        if path.is_file():
            paths.add(path)
            continue
        for directory, subdirs, files in os.walk(path):
            folder = Path(directory)
            subdirs[:] = [name for name in subdirs
                          if (name not in EXCLUDED_DIRS or folder / name == PYTHON_VENV_MODULE)
                          and not is_link(folder / name)]
            paths.update(folder / name for name in files if (folder / name).suffix not in EXCLUDED_SUFFIXES
                         and folder / name not in EXCLUDED_PATHS
                         and name != 'direct_url.json'
                         and not name.startswith(('.env', '.git-credentials')))
    return sorted(paths)


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, help='A new directory for the release files.')
    args = parser.parse_args()
    presentmon=ROOT/'runtime/presentmon/PresentMon-2.5.1-x64.exe'
    if not presentmon.is_file() or sha256(presentmon)!='9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191':
        raise ValueError('缺少或损坏的 PresentMon；请先运行 scripts\\run-python.cmd scripts\\download_presentmon.py')
    sources = package_files()
    target = args.destination.resolve()
    target.mkdir(parents=True)
    files = []
    launcher = build_launcher(target / LAUNCHER_NAME)
    files.append({'path': LAUNCHER_NAME, 'bytes': launcher.stat().st_size, 'sha256': sha256(launcher)})
    wheel_hashes = set()
    for source in sources:
        relative = source.relative_to(ROOT)
        if is_link(source) or not source.resolve().is_relative_to(ROOT):
            raise ValueError(f'Unexpected link: {relative}')
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, output)
        digest = sha256(output)
        files.append({'path': relative.as_posix(), 'bytes': output.stat().st_size, 'sha256': digest})
        if output.suffix == '.whl':
            wheel_hashes.add(digest)

    lock = (target / 'scripts/requirements-desktop-py311.txt').read_text(encoding='utf-8')
    for name, digest in re.findall(r'^(\S+) --hash=sha256:([0-9a-f]{64})$', lock, re.MULTILINE):
        if digest not in wheel_hashes:
            raise ValueError(f'Missing or modified wheel for {name}')
    manifest = {'files': files}
    (target / 'package-manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Staged {len(files)} files ({sum(row["bytes"] for row in files):,} bytes) in {target}')


if __name__ == '__main__':
    main()
