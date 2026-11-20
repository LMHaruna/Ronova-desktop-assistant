"""Prepare the pinned Intel PresentMon component for offline packaging."""
import hashlib
from pathlib import Path
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
URL='https://github.com/GameTechDev/PresentMon/releases/download/v2.5.1/PresentMon-2.5.1-x64.exe'
SHA256='9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191'
TARGET=ROOT/'runtime/presentmon/PresentMon-2.5.1-x64.exe'


def main():
    TARGET.parent.mkdir(parents=True,exist_ok=True)
    temporary=TARGET.with_name(TARGET.name+'.download-part')
    try:
        request=urllib.request.Request(URL,headers={'User-Agent':'Ronova-PresentMon-setup'})
        digest=hashlib.sha256()
        size=0
        with urllib.request.urlopen(request,timeout=60) as response, temporary.open('wb') as output:
            while block:=response.read(1024*1024):
                size+=len(block)
                if size>8*1024*1024: raise ValueError('PresentMon 下载文件异常地大')
                digest.update(block); output.write(block)
        if digest.hexdigest()!=SHA256:
            raise ValueError('PresentMon SHA-256 校验失败')
        temporary.replace(TARGET)
        print('PresentMon 已下载并通过 SHA-256 校验。')
    finally:
        temporary.unlink(missing_ok=True)


if __name__=='__main__':
    main()
