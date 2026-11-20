"""Compile the Windows GUI launcher without bundling the application runtime."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_NAME = 'Ronova.exe'
FRAMEWORK = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319'
ASSETS = ROOT / 'scripts/desktop-launcher'
ICON = ROOT / 'design/desktop-assistant-icons/Ronova.ico'


def build_launcher(output):
    output = Path(output).resolve()
    compiler = FRAMEWORK / 'csc.exe'
    if not compiler.is_file():
        raise FileNotFoundError('缺少 Windows .NET Framework 4.x 的 C# 编译器。')
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        binary = Path(directory) / LAUNCHER_NAME
        subprocess.run([
            str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
            f'/out:{binary}', f'/win32icon:{ICON}',
            f'/reference:{FRAMEWORK / "System.Windows.Forms.dll"}', str(ASSETS / 'Launcher.cs'),
        ], check=True)
        binary.replace(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / LAUNCHER_NAME)
    output = build_launcher(parser.parse_args().output)
    print(f'Built {output} ({output.stat().st_size:,} bytes)')


if __name__ == '__main__':
    main()
