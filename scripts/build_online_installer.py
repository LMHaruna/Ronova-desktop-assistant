"""Build a Windows x64 installer for a verified Ronova offline ZIP."""

import argparse
from hashlib import file_digest
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from urllib.parse import urlsplit
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'scripts/online-installer/Installer.cs'
FRAMEWORK = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319'


def release_metadata(archive, url):
    if url:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.fragment or any(char.isspace() for char in url)):
            raise ValueError('请提供完整、无需登录的 HTTPS 下载直链。')

    with archive.open('rb') as stream, ZipFile(stream) as package:
        members = [member for member in package.infolist() if not member.is_dir()]
        names = {PurePosixPath(member.filename) for member in members}
        roots = {name.parts[0] for name in names if name.parts}
        if len(roots) != 1 or any(name.is_absolute() or '..' in name.parts for name in names):
            raise ValueError('ZIP 必须包含一个 Ronova 发布目录。')
        folder = roots.pop()
        if not re.fullmatch(r'Ronova(?:-[A-Za-z0-9.]+)*-Windows-x64-Offline', folder):
            raise ValueError('ZIP 顶层目录须与 Ronova 发布目录命名一致。')
        required = {'VERSION', 'package-manifest.json', 'Start-Client.cmd', '安装.cmd',
                    'scripts/setup_desktop.py',
                    'runtime/python/cpython-3.11.17-windows-x86_64-none/python.exe'}
        if any(PurePosixPath(folder, file) not in names for file in required):
            raise ValueError('ZIP 缺少发布清单、安装入口或内置 Python。')
        unpacked = sum(member.file_size for member in members)
        stream.seek(0)
        digest = file_digest(stream, 'sha256').hexdigest()
        length = stream.tell()
    return '\n'.join((folder, digest, str(length), str(unpacked), url)) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path, help='已校验的单文件离线 ZIP')
    parser.add_argument('output', type=Path, help='新生成的 EXE 文件')
    parser.add_argument('--url', default='', help='资源 ZIP 的稳定 HTTPS 直链。省略时由安装者填写。')
    args = parser.parse_args()
    compiler = FRAMEWORK / 'csc.exe'
    output = args.output.resolve()
    if output.suffix.lower() != '.exe' or output.exists():
        parser.error('输出必须是尚未存在的 EXE 文件。')
    if not compiler.is_file():
        parser.error('缺少 Windows .NET Framework 4.x 的 C# 编译器。')
    data = release_metadata(args.archive, args.url)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        metadata = Path(directory) / 'release.txt'
        metadata.write_text(data, encoding='utf-8')
        references = ['System.Windows.Forms', 'System.Drawing', 'System.Net.Http', 'System.IO.Compression']
        subprocess.run([
            str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
            f'/out:{output}', f'/resource:{metadata},Ronova.Release',
            *(f'/reference:{FRAMEWORK / (name + ".dll")}' for name in references), str(SOURCE),
        ], check=True)
    print(f'Built {output} ({output.stat().st_size:,} bytes)')


if __name__ == '__main__':
    main()
