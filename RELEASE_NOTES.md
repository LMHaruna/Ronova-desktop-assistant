# Ronova Desktop Assistant v0.2.0

此版本加入桌面助手入口、Live2D 视频通话、Codex 工作区、截图标注和桌面整理，并更新主动回应、语音与本机状态显示。

## 安装

下载发布页中全部 `Ronova-0.2.0-Windows-x64-Offline.zip.001` 等分卷，以及 `Join-Ronova-Parts.ps1`，放入同一文件夹。在该文件夹中运行 PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Join-Ronova-Parts.ps1
```

合并程序会验证完整 ZIP 的 SHA-256；完成后解压 ZIP，双击 `安装.cmd`，再运行 `Start-Client.cmd`。运行包包含 Windows x64 Python 3.11 和离线依赖，无需另行安装 Python。下载、合并、解压和创建运行环境需要较多磁盘空间，建议预留至少 45 GiB。GitHub 自动生成的 Source code 压缩包不包含运行时和模型。

使用陪伴对话前，请在设置中配置自己的模型服务 API Key。Codex 工作区需要本机可用的 Codex 客户端或 CLI 并登录。显卡和模型要求详见 README。
