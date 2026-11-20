# Ronova Desktop Assistant v0.2.1

## 在线安装

下载并双击 `Ronova-Setup.exe`，选择空目录后点击「安装」。安装器会从本 Release 顺序下载资源分卷并校验 SHA-256，随后解压并配置内置的 Python 环境。下载中断时，使用相同的安装目录重试可续传。建议预留至少 45 GiB。

- 支持 Windows x64。安装需要联网访问 GitHub Releases，并使用 Windows 的 .NET Framework 4.8。
- 发布资源中的 `.zip.001` 等文件供安装器自动下载，`SHA256SUMS.json` 可查阅文件大小及 SHA-256。
- 安装完成后，从安装目录运行 `Ronova.exe`。陪伴模型密钥和 Codex 登录可在客户端设置中配置。

## 本版更新

- Codex 工作区支持右侧吸附、折叠、预览图自动展开及预览标记。
- 陪伴系统支持长期记忆摘要和自然语言修改，并减少主动回应复述近期聊天的情况。
- 桌面头像可显示能采集到的游戏帧率。视频通话使用静态立绘。

## 历史版本 v0.2.0

此版本加入桌面助手入口、Live2D 视频通话、Codex 工作区、截图标注和桌面整理，并更新主动回应、语音与本机状态显示。

## 安装

下载发布页中全部 `Ronova-0.2.0-Windows-x64-Offline.zip.001` 等分卷，以及 `Join-Ronova-Parts.ps1`，放入同一文件夹。在该文件夹中运行 PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Join-Ronova-Parts.ps1
```

合并程序会验证完整 ZIP 的 SHA-256；完成后解压 ZIP，双击 `安装.cmd`，再运行 `Start-Client.cmd`。运行包包含 Windows x64 Python 3.11 和离线依赖，无需另行安装 Python。下载、合并、解压和创建运行环境需要较多磁盘空间，建议预留至少 45 GiB。GitHub 自动生成的 Source code 压缩包不包含运行时和模型。

使用陪伴对话前，请在设置中配置自己的模型服务 API Key。Codex 工作区需要本机可用的 Codex 客户端或 CLI 并登录。显卡和模型要求详见 README。
