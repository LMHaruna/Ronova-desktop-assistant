"""Per-user Windows login startup for the bundled desktop client."""
from pathlib import Path
import subprocess
import winreg

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
VALUE_NAME = 'Ronova'


class WindowsAutostart:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def command(self):
        python = self.root/'runtime/voice-venv/Scripts/pythonw.exe'
        script = self.root/'client_app.py'
        if not python.is_file() or not script.is_file():
            raise RuntimeError('开机自动启动设置失败，请先完成客户端环境安装。')
        return subprocess.list2cmdline([str(python), '-EsB', '-X', 'utf8', str(script), '--autostart'])

    def set_enabled(self, enabled):
        if type(enabled) is not bool: raise ValueError('开机自动启动配置无效')
        try:
            if enabled:
                command = self.command()
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,RUN_KEY,0,
                                        winreg.KEY_QUERY_VALUE|winreg.KEY_SET_VALUE) as key:
                    try: current,kind = winreg.QueryValueEx(key,VALUE_NAME)
                    except FileNotFoundError: current,kind = None,None
                    if current!=command or kind!=winreg.REG_SZ:
                        winreg.SetValueEx(key,VALUE_NAME,0,winreg.REG_SZ,command)
            else:
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,RUN_KEY,0,winreg.KEY_SET_VALUE) as key:
                        winreg.DeleteValue(key,VALUE_NAME)
                except FileNotFoundError: pass
        except OSError as exc:
            raise RuntimeError('开机自动启动设置失败，请检查 Windows 启动项权限后重试。') from exc
