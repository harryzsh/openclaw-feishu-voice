# OpenClaw 飞书语音架构

> 实现飞书群聊中的完整语音交互：语音消息输入 → AI 理解 → AI 回复 → 语音消息输出

---

## 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                         飞书群聊                             │
│  用户发语音消息  ────────────────────→  收到语音回复         │
└──────────┬──────────────────────────────────▲───────────────┘
           │ Webhook (音频附件)                │ 飞书 IM API 发送 opus
           ▼                                  │
┌──────────────────────┐              ┌───────┴──────────────┐
│   OpenClaw Gateway   │              │   Polly Proxy        │
│   (Node.js 进程)     │              │   localhost:8080      │
│                      │              │                      │
│  1. 收到音频附件      │              │  - 兼容 OpenAI TTS   │
│  2. 调用 transcribe  │              │    API 格式           │
│     .py 转录         │              │  - 调用 AWS Polly     │
│  3. 文字送 AI 模型   │              │    生成 MP3           │
│  4. AI 生成回复文字  │              │  - FFmpeg 转 opus     │
│  5. 调用 TTS 工具    │              │  - 上传飞书发送        │
└──────────┬───────────┘              └───────▲──────────────┘
           │                                  │
           │  python3 transcribe.py           │  POST /v1/audio/speech
           ▼                                  │
┌──────────────────────┐          ┌───────────┴──────────────┐
│  AWS Transcribe      │          │       AWS Polly           │
│  Streaming           │          │                          │
│  (语音 → 文字)        │          │  Voice: Zhiyu (中文)     │
│  Language: zh-CN     │          │  Engine: neural          │
└──────────────────────┘          │  Format: mp3             │
                                  └──────────────────────────┘
```

---

## 完整流程详解

### 语音输入流程（Transcribe STT）

1. 用户在飞书发送语音消息
2. 飞书推送 Webhook 到 OpenClaw，携带 `file_key` 和 `duration`
3. OpenClaw 下载音频文件（飞书 opus 格式）
4. 调用 `scripts/transcribe.py <audio_file>`：
   - FFmpeg 将音频转为 16kHz PCM（s16le 格式）
   - 流式推送给 AWS Transcribe Streaming API
   - 返回转录文字到 stdout
5. OpenClaw 拿到文字，当做普通文本消息处理
6. 发送给 AI 模型（Claude / Bedrock）生成回复

### 语音输出流程（Polly TTS）

1. AI 生成文字回复
2. OpenClaw `tts()` 工具向 `http://localhost:8080/v1/audio/speech` 发 POST
3. Polly Proxy（`polly_proxy.py`）接收请求：
   - 调用 AWS Polly `synthesize_speech(VoiceId="Zhiyu", Engine="neural")`
   - 获得 MP3 音频流
   - 异步任务：FFmpeg 将 MP3 转为 opus → 上传飞书 → 发送语音消息气泡
   - 同时将 MP3 流返回给 OpenClaw（兼容 OpenAI TTS 格式）

---

## 目录结构

```
openclaw-feishu-voice/
├── README.md                  # 本文档
├── polly_proxy.py             # AWS Polly TTS 代理服务（FastAPI）
├── requirements-polly.txt     # Polly 代理依赖
├── scripts/
│   └── transcribe.py          # AWS Transcribe 语音转文字脚本
└── requirements-transcribe.txt # Transcribe 脚本依赖
```

---

## 环境要求

- Python 3.9+
- FFmpeg（系统级安装）
- AWS 凭证（IAM 权限：`transcribe:*` + `polly:SynthesizeSpeech`）
- OpenClaw Gateway

```bash
# 安装 FFmpeg
sudo apt-get install -y ffmpeg

# 配置 AWS 凭证
aws configure
# 或使用 EC2 IAM Role（推荐）
```

---

## 安装与启动

### 1. 安装 Polly Proxy 依赖

```bash
pip install -r requirements-polly.txt
```

### 2. 安装 Transcribe 脚本依赖

```bash
pip install -r requirements-transcribe.txt
```

### 3. 启动 Polly Proxy

```bash
python3 polly_proxy.py
# 服务启动在 http://localhost:8080
```

建议用 systemd 管理：

```ini
# /etc/systemd/system/polly-proxy.service
[Unit]
Description=Polly TTS Proxy
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/ubuntu/polly-proxy/main.py
WorkingDirectory=/home/ubuntu/polly-proxy
Restart=always
User=ubuntu

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable polly-proxy
sudo systemctl start polly-proxy
```

---

## OpenClaw 配置

在 `~/.openclaw/openclaw.json` 中配置以下关键字段：

### 音频转录配置（STT）

```json
"tools": {
  "media": {
    "audio": {
      "enabled": true,
      "echoTranscript": false,
      "models": [
        {
          "type": "cli",
          "command": "python3",
          "args": ["/home/ubuntu/clawd/scripts/transcribe.py", "{{MediaPath}}"],
          "timeoutSeconds": 30
        }
      ]
    }
  }
}
```

- `{{MediaPath}}` 是 OpenClaw 自动替换的音频文件路径占位符
- `echoTranscript: false` 表示不在消息里回显转录文字

### TTS 语音输出配置

```json
"messages": {
  "tts": {
    "auto": "always",
    "provider": "openai",
    "openai": {
      "apiKey": "dummy-not-needed",
      "baseUrl": "http://localhost:8080/v1",
      "model": "polly",
      "voice": "Zhiyu"
    }
  }
}
```

- `provider: "openai"` — OpenClaw 使用 OpenAI TTS 兼容格式
- `baseUrl` 指向本地 Polly Proxy（端口 8080）
- `voice: "Zhiyu"` — AWS Polly 中文女声（神经网络引擎）
- `apiKey: "dummy-not-needed"` — 本地代理不需要真实 key

### 飞书插件配置

```json
"channels": {
  "feishu": {
    "enabled": true,
    "accounts": {
      "default": {
        "appId": "YOUR_FEISHU_APP_ID",
        "appSecret": "YOUR_FEISHU_APP_SECRET",
        "botName": "Jarvis"
      }
    }
  }
}
```

---

## AWS IAM 权限要求

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "transcribe:StartStreamTranscription"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "polly:SynthesizeSpeech"
      ],
      "Resource": "*"
    }
  ]
}
```

---

## 可用的 Polly 中文声音

| VoiceId | 语言 | 引擎 | 说明 |
|---------|------|------|------|
| Zhiyu   | zh-CN | neural | 中文普通话女声（推荐）|
| Hiujin  | zh-HK | neural | 粤语女声 |

---

## 测试

```bash
# 测试 Polly Proxy
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input": "你好，我是语音助手", "voice": "Zhiyu"}' \
  --output test.mp3

# 测试 Transcribe 脚本
python3 scripts/transcribe.py /path/to/audio.opus --lang zh-CN

# 健康检查
curl http://localhost:8080/health
```

---

## 注意事项

1. **飞书音频格式**：飞书发送的语音为 opus 格式，FFmpeg 可直接处理
2. **Transcribe 区域**：`transcribe.py` 默认使用 `us-east-1`，可按需修改
3. **Polly 字符限制**：`polly_proxy.py` 中 `MAX_TTS_CHARS=200`，超过则不发飞书语音气泡（仍返回音频流）
4. **AWS 凭证**：建议在 EC2 上使用 IAM Role，无需配置 access key

---

## 技术栈

- **OpenClaw** — AI Agent 框架，处理消息路由和工具调用
- **AWS Transcribe Streaming** — 实时语音转文字
- **AWS Polly** — 神经网络 TTS（文字转语音）
- **FastAPI + uvicorn** — Polly Proxy 服务
- **FFmpeg** — 音频格式转换
- **飞书开放平台** — 消息收发和文件管理
