"""Run Ronova's offline setup, tests, and verification from one entry point."""

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
RUNTIME_PYTHON = ROOT / 'runtime/voice-venv/Scripts/python.exe'
TEST_REPORT = ROOT / 'runtime/verification-records/desktop-local/regression.json'
MANIFEST = ROOT / 'package-manifest.json'
ACTIONS = {
    '1': ('repair', '修复环境（重新安装依赖，请先退出 Ronova）'),
    '2': ('test', '运行回归测试'),
    '3': ('verify-runtime', '检查运行环境'),
    '4': ('verify-package', '校验发布包'),
}


def run_script(python, script, *args):
    try:
        return subprocess.run([str(python), '-EsB', '-X', 'utf8', str(SCRIPTS / script),
                               *(str(arg) for arg in args)], cwd=ROOT).returncode
    except OSError as error:
        print(f'无法启动维护脚本：{error}', file=sys.stderr)
        return 1


def run_action(action, args):
    if action == 'repair':
        return run_script(sys.executable, 'setup_desktop.py', '--ensure', '--reinstall', *args)
    if action == 'verify-package':
        return run_script(sys.executable, 'verify_package.py')
    status = run_script(sys.executable, 'setup_desktop.py', '--ensure')
    if status:
        return status
    if action == 'test':
        return run_script(RUNTIME_PYTHON, 'run_offline_checks.py', ROOT, TEST_REPORT, *args)
    return run_script(RUNTIME_PYTHON, 'verify_desktop_migration.py', *args)


def menu():
    while True:
        choices = {key: value for key, value in ACTIONS.items() if key != '4' or MANIFEST.is_file()}
        print('\nRonova 维护工具')
        for key, (_, label) in choices.items():
            print(f'  {key}. {label}')
        print('  0. 退出')
        try:
            choice = input('请选择：').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice == '0':
            return 0
        if choice not in choices:
            print('请输入菜单中的编号。')
            continue
        action, label = choices[choice]
        status = run_action(action, ())
        print(f'{label}完成。' if status == 0 else f'操作失败（退出码 {status}），请查看上方输出。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=[action for action, _ in ACTIONS.values()])
    parser.add_argument('args', nargs=argparse.REMAINDER, help='传给所选维护脚本的选项')
    options = parser.parse_args()
    if options.action == 'verify-package' and options.args:
        parser.error('verify-package 无需额外参数')
    return menu() if options.action is None else run_action(options.action, options.args)


if __name__ == '__main__':
    sys.exit(main())
