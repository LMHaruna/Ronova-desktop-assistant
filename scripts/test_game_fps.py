"""Game identification and displayed-frame telemetry without starting a game."""
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from client_game_fps import FrameSamples, GameFpsCapture, GameProcess, is_game_path, normalized, valid_presentmon
from client_hardware import HardwareReading, HardwareSampler
from client_shell import hardware_rows
from build_desktop_package import PACKAGE_PATHS


class GameRecognitionTests(unittest.TestCase):
    def test_steam_epic_and_registered_executables(self):
        steam=r'E:\SteamLibrary\steamapps\common\Example\game.exe'
        epic=normalized(r'D:\Epic Games\Example')
        registered=normalized(r'D:\Standalone\game.exe')
        self.assertTrue(is_game_path(steam,set(),set()))
        self.assertTrue(is_game_path(r'D:\Epic Games\Example\bin\game.exe',set(),{epic}))
        self.assertTrue(is_game_path(registered,{registered},set()))
        self.assertFalse(is_game_path(r'D:\Epic Games\ExampleOther\app.exe',set(),{epic}))
        self.assertFalse(is_game_path(r'C:\Windows\explorer.exe',set(),set()))
        self.assertFalse(is_game_path(r'E:\SteamLibrary\steamapps\common\Example\chrome.exe',set(),set()))

    def test_unverified_binary_is_never_used(self):
        with tempfile.TemporaryDirectory() as directory:
            fake=Path(directory)/'presentmon.exe'; fake.write_bytes(b'not the pinned binary')
            self.assertFalse(valid_presentmon(fake))
            self.assertFalse(valid_presentmon(fake.with_name('missing.exe')))

    def test_release_contains_the_frame_capture_component(self):
        self.assertIn('runtime/presentmon',PACKAGE_PATHS)


class FrameSampleTests(unittest.TestCase):
    def row(self, **values):
        return {'ProcessID':'42','SwapChainAddress':'0x01','Dropped':'0',
                'msBetweenDisplayChange':'16.6667',**values}

    def test_uses_displayed_frames_for_the_target_process(self):
        frames=FrameSamples(42)
        for time_value in (1,1.1,1.2): frames.accept(self.row(),now=time_value)
        frames.accept(self.row(ProcessID='43',msBetweenDisplayChange='2'),now=1.2)
        frames.accept(self.row(Dropped='1',msBetweenDisplayChange='2'),now=1.2)
        frames.accept(self.row(msBetweenDisplayChange='NA'),now=1.2)
        self.assertAlmostEqual(frames.fps(now=1.3),60,places=1)
        self.assertIsNone(frames.fps(now=2.0))

    def test_different_swapchains_are_not_averaged_together(self):
        frames=FrameSamples(42)
        for time_value in (1,1.1,1.2): frames.accept(self.row(),now=time_value)
        for time_value in (1,1.1): frames.accept(self.row(SwapChainAddress='0x02',msBetweenDisplayChange='50'),now=time_value)
        self.assertAlmostEqual(frames.fps(now=1.3),60,places=1)

    def test_no_games_or_unavailable_fps_hides_the_row(self):
        self.assertEqual([row[0] for row in hardware_rows(HardwareReading())],['CPU','GPU','内存'])
        rows=hardware_rows(HardwareReading(game_fps=59.7))
        self.assertEqual(rows[-1],('游戏','60 FPS',''))

    def test_capture_reads_target_display_data_and_stops_when_requested(self):
        output=io.StringIO('ProcessID,SwapChainAddress,Dropped,msBetweenDisplayChange\n'
                           '42,0x01,0,16.6667\n42,0x01,0,16.6667\n')
        process=Mock(stdout=output); process.poll.return_value=None
        with patch('client_game_fps.valid_presentmon',return_value=True), \
             patch('client_game_fps.subprocess.Popen',return_value=process) as popen, \
             patch('client_game_fps.subprocess.run'):
            capture=GameFpsCapture(GameProcess(42,1))
            deadline=time.monotonic()+2
            while capture.fps() is None and time.monotonic()<deadline: time.sleep(.01)
            self.assertAlmostEqual(capture.fps(),60,places=1)
            command=popen.call_args.args[0]
            self.assertEqual(command[command.index('--process_id')+1],'42')
            self.assertIn('--exclude_dropped',command)
            self.assertIn('--output_stdout',command)
            capture.stop()
            self.assertIsNone(capture.fps())

    def test_sampler_clears_fps_and_stops_capture_when_game_disappears(self):
        game=GameProcess(42,1)
        capture=Mock(); capture.fps.return_value=60.0
        stop=Mock(); stop.wait.side_effect=[False,False,True]
        sampler=HardwareSampler.__new__(HardwareSampler)
        sampler.stopped=stop; sampler.reading=HardwareReading()
        with patch('client_hardware.registered_games',return_value=(set(),set())), \
             patch('client_hardware.visible_game',side_effect=[game,game,None]), \
             patch('client_hardware.GameFpsCapture',return_value=capture), \
             patch('client_hardware.threading.Thread'), \
             patch('client_hardware.psutil.cpu_percent',return_value=20), \
             patch('client_hardware.psutil.virtual_memory',return_value=Mock(percent=40)):
            sampler.sample()
        self.assertIsNone(sampler.reading.game_fps)
        capture.stop.assert_called_once()


if __name__=='__main__': unittest.main()
