# 运行声音参考

此目录保留准备好的 TTS 样本。当前 IndexTTS-2.5 使用 indextts-primary-20s.wav（20 秒），对应“录音 (2).wav”的 0–20 秒，与四段对比试听中选中的参考一致。由 config/voice-selection.json 选择，并由 references.json 提供文本及相对路径。不要删除当前运行样本。

`asr-near-end.wav` 是本地语音的备用路径，也供流式识别验证和试听使用。`asr-far-end-echo.wav` 用于识别、回声验证和试听。`indextts-fallback.wav` 是 IndexTTS 缺少参考编号时的备用样本，也可用于试听。`qwen-voice-reference.wav` 由千问 TTS Next 合成请求和音色复刻工具使用。原始录音供样本重新裁切和转写使用。

更换本地样本或参考文本会影响声音；修改后需完整退出并重启。云端音色由 config/realtime-voice.json 单独控制，删除本地复刻记录不等于删除服务端资源。

IndexTTS 的参考读取长度由工作进程按样本时长传入，本样本完整读取 20 秒。参考文本和随机种子沿用所选试听设置。
