"""Sample local hardware and a visible game's frame rate while the card is open."""

from dataclasses import dataclass
import math
import os
from pathlib import Path
import subprocess
import threading

import psutil
from client_game_fps import GameFpsCapture, registered_games, visible_game


@dataclass(frozen=True)
class HardwareReading:
    cpu_load: float | None = None
    memory_load: float | None = None
    gpu_load: float | None = None
    gpu_temperature: float | None = None
    game_fps: float | None = None


SYSTEM32 = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'


def command_output(command, timeout):
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors='replace',
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout)
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def numeric(value, minimum, maximum):
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and minimum <= number <= maximum else None


def gpu_reading():
    for executable in (SYSTEM32 / 'nvidia-smi.exe',
                       Path(os.environ.get('ProgramFiles', r'C:\Program Files')) /
                       'NVIDIA Corporation/NVSMI/nvidia-smi.exe'):
        if executable.is_file():
            break
    else:
        return None, None
    output = command_output([str(executable), '--query-gpu=utilization.gpu,temperature.gpu',
                             '--format=csv,noheader,nounits', '--id=0'], timeout=1.2)
    fields = output.split(',')
    if len(fields) != 2:
        return None, None
    return numeric(fields[0], 0, 100), numeric(fields[1], 0, 125)


class HardwareSampler:
    def __init__(self):
        self.reading = HardwareReading()
        self.stopped = threading.Event()
        threading.Thread(target=self.sample, name='desktop-hardware', daemon=True).start()

    def stop(self):
        self.stopped.set()

    def sample(self):
        psutil.cpu_percent()
        self.gpu = (None, None)
        try:
            executables,folders=registered_games()
            game=visible_game(executables,folders)
        except (OSError,ImportError):
            executables,folders=set(),set(); game=None
        capture=GameFpsCapture(game) if game else None

        def sample_gpu():
            # Keep the driver query separate from local sampling and the GUI.
            while not self.stopped.is_set():
                self.gpu = gpu_reading()
                self.stopped.wait(1.5)

        threading.Thread(target=sample_gpu, name='desktop-gpu', daemon=True).start()
        while not self.stopped.wait(.5):
            active=visible_game(executables,folders)
            if active!=game:
                if capture: capture.stop()
                game=active
                capture=GameFpsCapture(game) if game else None
            fps=capture.fps() if capture else None
            gpu_load, gpu_temperature = self.gpu
            self.reading = HardwareReading(
                cpu_load=psutil.cpu_percent(), memory_load=psutil.virtual_memory().percent,
                gpu_load=gpu_load, gpu_temperature=gpu_temperature, game_fps=fps)
        if capture: capture.stop()
