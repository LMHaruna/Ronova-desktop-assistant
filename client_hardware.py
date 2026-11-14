"""Sample local hardware and Windows desktop composition while the card is open."""

import ctypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import struct
import subprocess
import threading
import time

import psutil


@dataclass(frozen=True)
class HardwareReading:
    cpu_load: float | None = None
    memory_load: float | None = None
    gpu_load: float | None = None
    gpu_temperature: float | None = None
    desktop_fps: float | None = None


# Packed DWM_TIMING_INFO from dwmapi.h. cFrame follows qpcCompose.
DWM_TIMING_SIZE = 292
DWM_FRAME_OFFSET = 56
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


def desktop_frame_count():
    try:
        query = ctypes.WinDLL('dwmapi.dll').DwmGetCompositionTimingInfo
    except (AttributeError, OSError):
        return None
    query.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    query.restype = ctypes.c_long
    timing = ctypes.create_string_buffer(DWM_TIMING_SIZE)
    struct.pack_into('<I', timing, 0, DWM_TIMING_SIZE)
    if query(None, timing) != 0:
        return None
    return struct.unpack_from('<Q', timing, DWM_FRAME_OFFSET)[0]


class HardwareSampler:
    def __init__(self):
        self.reading = HardwareReading()
        self.stopped = threading.Event()
        threading.Thread(target=self.sample, name='desktop-hardware', daemon=True).start()

    def stop(self):
        self.stopped.set()

    def sample(self):
        psutil.cpu_percent()
        last_frame = desktop_frame_count()
        last_time = time.monotonic()
        self.gpu = (None, None)

        def sample_gpu():
            # Keep the driver query separate from local sampling and the GUI.
            while not self.stopped.is_set():
                self.gpu = gpu_reading()
                self.stopped.wait(1.5)

        threading.Thread(target=sample_gpu, name='desktop-gpu', daemon=True).start()
        while not self.stopped.wait(.5):
            frame = desktop_frame_count()
            now = time.monotonic()
            fps = None
            if frame is not None and last_frame is not None and frame >= last_frame:
                fps = (frame - last_frame) / (now - last_time)
            gpu_load, gpu_temperature = self.gpu
            self.reading = HardwareReading(
                cpu_load=psutil.cpu_percent(), memory_load=psutil.virtual_memory().percent,
                gpu_load=gpu_load, gpu_temperature=gpu_temperature, desktop_fps=fps)
            last_frame, last_time = frame, now
