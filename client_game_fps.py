"""Identify visible games and read their displayed frames from PresentMon."""
from collections import deque
import csv
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

import psutil


ROOT = Path(__file__).resolve().parent
PRESENTMON = ROOT/'runtime/presentmon/PresentMon-2.5.1-x64.exe'
PRESENTMON_SHA256 = '9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191'
EXCLUDED_EXECUTABLES = {'steam.exe','steamwebhelper.exe','gameoverlayui.exe',
                        'epicgameslauncher.exe','galaxyclient.exe','galaxyclientservice.exe',
                        'explorer.exe','dwm.exe','chrome.exe','msedge.exe','firefox.exe',
                        'brave.exe','opera.exe','discord.exe','code.exe','python.exe','pythonw.exe'}


def normalized(path):
    return os.path.normcase(os.path.normpath(os.path.expandvars(str(path))))


def within(path, directory):
    try:
        return os.path.commonpath((path,directory)) == directory
    except ValueError:
        return False


def registered_games():
    """Read known game paths from Windows, Epic and GOG without changing them."""
    import winreg
    executables=set(); folders=set()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'System\GameConfigStore\Children') as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root,winreg.EnumKey(root,index)) as child:
                        value,_=winreg.QueryValueEx(child,'MatchedExeFullPath')
                        if isinstance(value,str) and value.lower().endswith('.exe'):
                            executables.add(normalized(value))
                except OSError:
                    continue
    except OSError:
        pass
    epic=Path(os.environ.get('ProgramData',r'C:\ProgramData'))/'Epic/EpicGamesLauncher/Data/Manifests'
    try:
        for manifest in epic.glob('*.item'):
            try:
                if manifest.stat().st_size>1024*1024: continue
                value=json.loads(manifest.read_text(encoding='utf-8-sig'))
                directory=value.get('InstallLocation'); executable=value.get('LaunchExecutable')
                if isinstance(directory,str) and directory and isinstance(executable,str) and executable:
                    folders.add(normalized(directory))
            except (OSError,ValueError,AttributeError):
                continue
    except OSError:
        pass
    for hive in (winreg.HKEY_LOCAL_MACHINE,winreg.HKEY_CURRENT_USER):
        for key in (r'SOFTWARE\WOW6432Node\GOG.com\Games',r'SOFTWARE\GOG.com\Games'):
            try:
                with winreg.OpenKey(hive,key) as root:
                    for index in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root,winreg.EnumKey(root,index)) as game:
                                path,_=winreg.QueryValueEx(game,'PATH')
                                if isinstance(path,str) and path: folders.add(normalized(path))
                        except OSError:
                            continue
            except OSError:
                continue
    return executables,folders


def is_game_path(path, executables, folders):
    path=normalized(path)
    if os.path.basename(path).lower() in EXCLUDED_EXECUTABLES: return False
    steam_marker=os.sep+'steamapps'+os.sep+'common'+os.sep
    return steam_marker in path or path in executables or any(within(path,folder) for folder in folders)


@dataclass(frozen=True)
class GameProcess:
    pid: int
    started: float


def visible_game(executables, folders):
    """Choose the uppermost visible, non-minimized registered game window."""
    try:
        user=ctypes.WinDLL('user32',use_last_error=True)
        dwm=ctypes.WinDLL('dwmapi',use_last_error=True)
        callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
        user.EnumWindows.argtypes=(callback_type,wintypes.LPARAM)
        user.EnumWindows.restype=wintypes.BOOL
        user.IsWindowVisible.argtypes=(wintypes.HWND,); user.IsWindowVisible.restype=wintypes.BOOL
        user.IsIconic.argtypes=(wintypes.HWND,); user.IsIconic.restype=wintypes.BOOL
        user.GetWindowRect.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.RECT)); user.GetWindowRect.restype=wintypes.BOOL
        user.GetWindowThreadProcessId.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.DWORD))
        dwm.DwmGetWindowAttribute.argtypes=(wintypes.HWND,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD)
        found=[]
        @callback_type
        def inspect(hwnd,_):
            if not user.IsWindowVisible(hwnd) or user.IsIconic(hwnd): return True
            cloaked=wintypes.DWORD()
            if dwm.DwmGetWindowAttribute(hwnd,14,ctypes.byref(cloaked),ctypes.sizeof(cloaked))==0 and cloaked.value:
                return True
            rect=wintypes.RECT()
            if not user.GetWindowRect(hwnd,ctypes.byref(rect)) or rect.right-rect.left<480 or rect.bottom-rect.top<270:
                return True
            pid=wintypes.DWORD(); user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
            if not pid.value or pid.value==os.getpid(): return True
            try:
                process=psutil.Process(pid.value)
                if is_game_path(process.exe(),executables,folders):
                    found.append(GameProcess(pid.value,process.create_time()))
                    return False
            except (psutil.Error,OSError):
                pass
            return True
        user.EnumWindows(inspect,0)
        return found[0] if found else None
    except (AttributeError,OSError):
        return None


def valid_presentmon(path=PRESENTMON):
    try:
        with path.open('rb') as stream:
            return hashlib.file_digest(stream,'sha256').hexdigest()==PRESENTMON_SHA256
    except OSError:
        return False


class FrameSamples:
    def __init__(self, pid):
        self.pid=pid; self.samples={}; self.lock=threading.Lock()

    def accept(self, row, now=None):
        try:
            if int(row['ProcessID'])!=self.pid: return
            if str(row.get('Dropped','')).lower() not in ('0','false'): return
            chain=row['SwapChainAddress']
            if not chain: return
            duration=float(row['msBetweenDisplayChange'])
            if not math.isfinite(duration) or not 0.5<=duration<=2000: return
        except (KeyError,TypeError,ValueError):
            return
        with self.lock:
            if chain not in self.samples and len(self.samples)>=32: return
            self.samples.setdefault(chain,deque(maxlen=1000)).append(
                (time.monotonic() if now is None else now,duration))

    def fps(self, now=None):
        now=time.monotonic() if now is None else now
        with self.lock:
            for chain in tuple(self.samples):
                samples=self.samples[chain]
                while samples and now-samples[0][0]>1.5: samples.popleft()
                if not samples: del self.samples[chain]
            if not self.samples: return None
            samples=max(self.samples.values(),key=len)
            if len(samples)<2 or now-samples[-1][0]>.75: return None
            return 1000*len(samples)/sum(duration for _,duration in samples)


class GameFpsCapture:
    def __init__(self, game):
        self.game=game; self.samples=FrameSamples(game.pid); self.process=None
        self.stopped=threading.Event(); self.session='Ronova-'+uuid.uuid4().hex
        threading.Thread(target=self.capture,name='game-fps',daemon=True).start()

    def capture(self):
        if not valid_presentmon() or self.stopped.is_set(): return
        command=[str(PRESENTMON),'--process_id',str(self.game.pid),'--output_stdout','--no_console_stats',
                 '--v1_metrics','--no_track_gpu','--no_track_input','--exclude_dropped',
                 '--timed','7','--terminate_after_timed','--terminate_on_proc_exit','--session_name',self.session]
        try:
            self.process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                text=True,encoding='utf-8-sig',errors='replace',bufsize=1,creationflags=subprocess.CREATE_NO_WINDOW)
            if self.stopped.is_set(): self.stop_session()
            for row in csv.DictReader(self.process.stdout):
                if not self.stopped.is_set(): self.samples.accept(row)
            self.process.wait(timeout=2)
        except (OSError,ValueError,subprocess.TimeoutExpired):
            if self.process and self.process.poll() is None: self.process.kill()

    def fps(self):
        return None if self.stopped.is_set() else self.samples.fps()

    def stop(self):
        self.stopped.set()
        if self.process and self.process.poll() is None:
            threading.Thread(target=self.stop_session,name='game-fps-stop',daemon=True).start()

    def stop_session(self):
        try:
            subprocess.run([str(PRESENTMON),'--terminate_existing_session','--session_name',self.session],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW,timeout=4)
        except (OSError,subprocess.TimeoutExpired):
            pass
