"""Build Ronova's isolated desktop environment from the included wheels."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import time
import venv

ROOT=Path(__file__).resolve().parents[1]
ENV=ROOT/'runtime/voice-venv'
PYTHON=ENV/'Scripts/python.exe'
PYTHONW=ENV/'Scripts/pythonw.exe'
MARKER=ENV/'.desktop-env.json'
LOCK=ROOT/'scripts/requirements-desktop-py311.txt'
WHEELS=ROOT/'runtime/wheelhouse'
CUDA_WHEELS=ROOT/'runtime/wheelhouse-cuda'


def run(command):
    subprocess.run([str(arg) for arg in command],cwd=ROOT,check=True)


def require_base_python():
    if (sys.implementation.name != 'cpython' or sys.version_info[:2] != (3, 11)
            or sysconfig.get_platform() != 'win-amd64' or sys.prefix != sys.base_prefix):
        raise RuntimeError('请使用已安装的 Windows x64 CPython 3.11 基础解释器。')


def setup(reinstall):
    base_python=Path(sys.executable).resolve()
    lock_hash=hashlib.sha256(LOCK.read_bytes()).hexdigest()
    expected={'root':str(ROOT),'base_python':str(base_python),'lock_sha256':lock_hash,
              'python':sys.version.split()[0]}
    try: current=json.loads(MARKER.read_text(encoding='utf-8'))
    except (OSError,ValueError): current={}
    if not isinstance(current, dict):
        current={}
    if (not reinstall and PYTHON.is_file() and PYTHONW.is_file()
            and all(current.get(k)==v for k,v in expected.items())):
        print('Ronova 的 Python 3.11 环境已就绪。',flush=True)
        return 0
    for folder in (WHEELS, CUDA_WHEELS):
        if not folder.is_dir():
            raise FileNotFoundError(f'缺少离线依赖目录，请完整解压发布包：{folder}')

    cfg=ENV/'pyvenv.cfg'
    cfg_lines=cfg.read_text(encoding='utf-8').casefold().splitlines() if cfg.is_file() else []
    expected_home=f'home = {base_python.parent}'.casefold()
    reusable=(not reinstall and PYTHON.is_file() and PYTHONW.is_file()
              and expected_home in cfg_lines and current.get('root', str(ROOT)) == str(ROOT)
              and current.get('python', expected['python']) == expected['python'])
    # Preserve older environments for recovery when the interpreter or project path changes.
    if ENV.exists() and not reusable:
        backup=ENV.with_name(f'voice-venv.previous-{time.time_ns()}')
        ENV.rename(backup)
        print(f'旧环境已备份到 {backup}',flush=True)
    MARKER.unlink(missing_ok=True)
    temporary=ROOT/'runtime/setup-tmp'
    temporary.mkdir(parents=True,exist_ok=True)
    for name in list(os.environ):
        if name.upper().startswith('PIP_') or name.upper() in ('PYTHONHOME', 'PYTHONPATH'):
            os.environ.pop(name)
    os.environ.update(PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',
                      PIP_CONFIG_FILE=os.devnull,PIP_NO_INDEX='1',
                      PIP_DISABLE_PIP_VERSION_CHECK='1',PIP_NO_CACHE_DIR='1',
                      TEMP=str(temporary),TMP=str(temporary))
    print('正在从随包 wheel 准备 Ronova 的 Python 3.11 环境，首次安装可能需要几分钟。',flush=True)
    if not reusable: venv.EnvBuilder(with_pip=True).create(ENV)
    run([PYTHON,'-m','pip','install','--no-index','--find-links',WHEELS,
         '--find-links',CUDA_WHEELS,'--only-binary=:all:',
         '--no-deps','--no-compile','--require-hashes','-r',LOCK])
    run([PYTHON,'-m','pip','check'])
    # Native libraries and speech resources must load before marking setup complete.
    run([PYTHON,ROOT/'scripts/verify_desktop_migration.py','--quick'])
    pending_marker=MARKER.with_suffix('.tmp')
    pending_marker.write_text(json.dumps(dict(expected,created=time.strftime('%Y-%m-%d %H:%M:%S')),
                                        ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    pending_marker.replace(MARKER)
    print('环境安装完成。',flush=True)
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ensure',action='store_true')
    parser.add_argument('--reinstall',action='store_true')
    parser.add_argument('--no-pause',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    require_base_python()
    if ROOT.drive and len(str(ROOT))>135:
        raise RuntimeError('请解压到较短的路径，例如 D:/Ronova。')
    if not ENV.parent.is_dir():
        raise FileNotFoundError('缺少 runtime 目录，请完整解压发布包。')
    import msvcrt

    with (ENV.parent/'.desktop-setup.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError('另一个 Ronova 环境安装正在进行，请等待它结束后再启动。') from error
        return setup(args.reinstall)


if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as error:
        print(f'环境安装失败（{type(error).__name__}）{error}',file=sys.stderr)
        raise SystemExit(1)
