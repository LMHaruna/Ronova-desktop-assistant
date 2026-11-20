"""Build a Windows x64 installer for a verified Ronova offline ZIP."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from urllib.parse import quote, urlsplit
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'scripts/online-installer/Installer.cs'
FRAMEWORK = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319'
GITHUB_PART_SIZE = 1900 * 1024 * 1024


def archive_metadata(archive, url, repo, output_dir):
    if url:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.fragment or any(char.isspace() for char in url)):
            raise ValueError('请提供完整、无需登录的 HTTPS 下载直链。')

    with ZipFile(archive) as package:
        members = [member for member in package.infolist() if not member.is_dir()]
        names = {PurePosixPath(member.filename) for member in members}
        roots = {name.parts[0] for name in names if name.parts}
        if len(roots) != 1 or any(name.is_absolute() or '..' in name.parts for name in names):
            raise ValueError('ZIP 必须包含一个 Ronova 发布目录。')
        folder = roots.pop()
        if not re.fullmatch(r'Ronova(?:-[A-Za-z0-9.]+)*-Windows-x64-Offline', folder):
            raise ValueError('ZIP 顶层目录须与 Ronova 发布目录命名一致。')
        required = {'VERSION', 'package-manifest.json', '安装.cmd',
                    'scripts/setup_desktop.py',
                    'runtime/python/cpython-3.11.17-windows-x86_64-none/python.exe'}
        if any(PurePosixPath(folder, file) not in names for file in required):
            raise ValueError('ZIP 缺少发布清单、安装入口或内置 Python。')
        if not any(PurePosixPath(folder, name) in names for name in ('Ronova.exe', 'Start-Client.cmd')):
            raise ValueError('ZIP 缺少客户端启动入口。')
        unpacked = sum(member.file_size for member in members)
        version = package.read(f'{folder}/VERSION').decode('ascii').strip()
    if repo and (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)
                 or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version)
                 or folder != f'Ronova-{version}-Windows-x64-Offline'):
        raise ValueError('GitHub 模式需要有效的仓库名和与 VERSION 一致的发布目录。')

    base = f'https://github.com/{repo}/releases/download/v{version}/'
    digest = sha256()
    parts = []
    length = archive.stat().st_size
    with archive.open('rb') as stream:
        for index, offset in enumerate(range(0,length,GITHUB_PART_SIZE if repo else length),1):
            remaining = min(GITHUB_PART_SIZE,length-offset) if repo else length
            if not repo:
                for data in iter(lambda: stream.read(4*1024*1024),b''):
                    digest.update(data)
                break
            name = f'{folder}.zip.{index:03}'
            part_digest = sha256()
            with (output_dir/name).open('xb') as output:
                size = remaining
                while remaining:
                    data = stream.read(min(4*1024*1024,remaining))
                    if not data:
                        raise OSError('ZIP 在生成分卷时被截断。')
                    output.write(data)
                    part_digest.update(data)
                    digest.update(data)
                    remaining -= len(data)
            parts.append({'name':name,'bytes':size,'sha256':part_digest.hexdigest(),'url':base+quote(name)})
        if stream.tell() != length or stream.read(1):
            raise OSError('ZIP 在构建期间改变。')
    manifest = {'version':version,'archive':{'name':folder+'.zip','bytes':length,'sha256':digest.hexdigest()},
                'installed_bytes':unpacked,'parts':parts}
    rows = [folder,digest.hexdigest(),str(length),str(unpacked),url]
    rows.extend(f"{part['sha256']}\t{part['bytes']}\t{part['url']}" for part in parts)
    return '\n'.join(rows)+'\n',manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path, help='已校验的单文件离线 ZIP')
    parser.add_argument('output', type=Path, help='新生成的 EXE 文件')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--url', default='', help='单文件 ZIP 的稳定 HTTPS 直链')
    source.add_argument('--github-repo', help='OWNER/REPO。在 EXE 同目录生成 GitHub Releases 分卷。')
    args = parser.parse_args()
    compiler = FRAMEWORK / 'csc.exe'
    output = args.output.resolve()
    if output.suffix.lower() != '.exe' or output.exists():
        parser.error('输出必须是尚未存在的 EXE 文件。')
    if not compiler.is_file():
        parser.error('缺少 Windows .NET Framework 4.x 的 C# 编译器。')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        staging = Path(directory)
        data,manifest = archive_metadata(args.archive,args.url,args.github_repo,staging)
        metadata = staging / 'release.txt'
        metadata.write_text(data, encoding='utf-8')
        executable = staging / output.name
        references = ['System.Windows.Forms', 'System.Drawing', 'System.Net.Http', 'System.IO.Compression']
        subprocess.run([
            str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
            f'/out:{executable}', f'/resource:{metadata},Ronova.Release',
            f'/win32icon:{ROOT / "design/desktop-assistant-icons/Ronova.ico"}',
            *(f'/reference:{FRAMEWORK / (name + ".dll")}' for name in references), str(SOURCE),
        ], check=True)
        assets = [executable]
        if manifest['parts']:
            assets += [staging / part['name'] for part in manifest['parts']]
            manifest['installer'] = {'name':output.name,'bytes':executable.stat().st_size,
                                     'sha256':sha256(executable.read_bytes()).hexdigest()}
            index = staging / 'SHA256SUMS.json'
            index.write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
            assets.append(index)
        if any((output.parent / asset.name).exists() for asset in assets):
            raise FileExistsError('发布目录已有同名文件。')
        for asset in assets:
            asset.rename(output.parent / asset.name)
    print(f'Built {output} ({output.stat().st_size:,} bytes)')


if __name__ == '__main__':
    main()
