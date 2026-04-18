# OpenClaw 飞书语音架构

> 实现飞书群聊中的完整语音交互：语音消息输入 → AI 理解 → AI 回复 → 语音消息输出

**Current version:** `v2.0.0` — synced from production (EC2 `/home/ubuntu/transcribe_polly/`)

---

## 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                         飞书群聊                             │
│  用户发语音消息  ────────────────────→  收到语音气泡回复      │
└──────────┬──────────────────────────────────▲───────────────┘
           │ Webhook (音频附件)                │ 飞书 IM API（opus 语音气泡）
           ▼                                  │
┌──────────────────────┐              ┌───────┴──────────────┐
│   OpenClaw Gateway   │──────────────▶   OpenClaw TTS       │
│   (Node.js 进程)     │  内置语音气泡  │   (原生支持飞书)      │
│                      │  发送逻辑     │                      │
│  1. 收到音频附件      │              │  VOICE_BUBBLE_CHANNELS│
│  2. 调用 transcribe  │              │  包含 feishu，自动     │
│     .py 转录         │              │  转 opus 发语音气泡   │
│  3. 文字送 AI 模型   │              └───────▲──────────────┘
│  4. AI 生成回复文字  │                      │
│  5. 调用 TTS 工具    │                      │ POST /v1/audio/speech (mp3)
└──────────┬───────────┘              ┌───────┴──────────────┐
           │                          │   Polly Proxy        │
           │  python3 transcribe.py   │   localhost:8080      │
           ▼                          │   (polly_proxy.py)   │
┌──────────────────────┐              │                      │
│  AWS Transcribe      │              │  - 兼容 OpenAI TTS   │
│  Streaming           │              │    API 格式           │
│  (语音 → 文字)        │              │  - 调用 AWS Polly     │
│  Language: zh-CN     │              │    生成 MP3 返回      │
└──────────────────────┘              │  - 可选：同步发       │
                                       │    飞书语音气泡      │
                                       └──────────────────────┘
                                               │
                                       ┌───────┴──────────────┐
                                       │       AWS Polly       │
                                       │  Voice: Zhiyu (中文)  │
                                       │  Engine: neural       │
                                       │  Format: mp3          │
                                       └──────────────────────┘
```

**飞书语音气泡由 OpenClaw 原生处理**：OpenClaw 内置 `VOICE_BUBBLE_CHANNELS`（包含 `feishu`），收到 TTS 音频后自动转 opus 发送语音气泡，无需 Polly Proxy 介入。

---

## 完整流程详解

### 语音输入流程（Transcribe STT）

1. 用户在飞书发送语音消息
2. 飞书推送 Webhook 到 OpenClaw，携带 `file_key` 和 `duration`
3. OpenClaw 下载音频文件（飞书 opus 格式）
4. 调用 `transcribe.py <audio_file>`：
   - FFmpeg 将音频转为 16kHz PCM（`s16le` 格式）
   - 流式推送给 AWS Transcribe Streaming API
   - 返回转录文字到 stdout
5. OpenClaw 拿到文字，当做普通文本消息处理
6. 发送给 AI 模型（Claude / Bedrock）生成回复

### 语音输出流程（Polly TTS）

1. AI 生成文字回复
2. OpenClaw `tts()` 工具向 `http://localhost:8080/v1/audio/speech` 发 POST
3. `polly_proxy.py` 接收请求，调用 AWS Polly 生成 MP3，返回音频流
4. **OpenClaw 原生处理**：检测到当前 channel 为 `feishu`（在 `VOICE_BUBBLE_CHANNELS` 内），自动将 MP3 转 opus 并以语音气泡形式发送给用户

---

## 目录结构（v2.0.0 扁平化）

```
openclaw-feishu-voice/
├── README.md                         # 本文档
├── polly_proxy.py                    # AWS Polly TTS 代理（FastAPI，v2.0.0）
├── transcribe.py                     # AWS Transcribe 流式转录脚本
├── requirements.txt                  # Python 依赖（proxy + transcribe）
├── config/
│   └── openclaw-audio-tts.json       # OpenClaw 音频/TTS 配置片段示例
└── hooks/
    └── polly-voice-reply/
        └── handler.ts                # 可选 OpenClaw hook（发送飞书语音气泡）
```

> v1 的 `polly-proxy/main.py`、`scripts/transcribe.py`、`requirements-*.txt` 已合并到根目录。

---

## 环境要求

- Python 3.9+
- FFmpeg（系统级安装）
- AWS 凭证（IAM 权限：`transcribe:StartStreamTranscription` + `polly:SynthesizeSpeech`）
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

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动 Polly Proxy

```bash
python3 polly_proxy.py
# 服务启动在 http://localhost:8080
```

建议用 systemd 管理（生产 EC2 实际使用的 unit）：

```ini
# ~/.config/systemd/user/polly-proxy.service
[Unit]
Description=Polly TTS Proxy
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/transcribe_polly
ExecStart=/usr/bin/python3 /home/ubuntu/transcribe_polly/polly_proxy.py
Restart=always
RestartSec=5
StandardOutput=append:/tmp/polly-proxy.log
StandardError=append:/tmp/polly-proxy.log

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable polly-proxy
systemctl --user start polly-proxy
systemctl --user status polly-proxy
```

---

## OpenClaw 配置

在 `~/.openclaw/openclaw.json` 中配置以下关键字段（完整示例见 [`config/openclaw-audio-tts.json`](config/openclaw-audio-tts.json)）：

### 音频转录配置（STT）

```json
"tools": {
  "media": {
    "audio": {
      "enabled": true,
      "echoTranscript": false,
      "echoFormat": "📝 \"{transcript}\"",
      "models": [
        {
          "type": "cli",
          "command": "python3",
          "args": ["/home/ubuntu/transcribe_polly/transcribe.py", "{{MediaPath}}"],
          "timeoutSeconds": 30
        }
      ]
    }
  }
}
```

- `echoTranscript` 为 `true` 时会把转录文字回显给用户
- `echoFormat` 自定义回显模板（`{transcript}` 被替换为识别文字）

### TTS 语音输出配置（新格式：`providers.openai`）

```json
"messages": {
  "tts": {
    "auto": "always",
    "provider": "openai",
    "providers": {
      "openai": {
        "apiKey": "dummy-not-needed",
        "baseUrl": "http://localhost:8080/v1",
        "model": "polly",
        "voice": "Zhiyu"
      }
    }
  }
}
```

- `provider: "openai"` — OpenClaw 使用 OpenAI TTS 兼容格式
- `providers.openai.baseUrl` 指向本地 Polly Proxy（端口 8080）
- `voice: "Zhiyu"` — AWS Polly 中文女声（神经网络引擎）
- 飞书 channel 自动触发语音气泡（无需额外配置）

> ⚠️ v1 旧格式 `"openai": {...}`（与 `provider` 平级）已废弃；请改为 `"providers": {"openai": {...}}`。

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
      "Action": ["transcribe:StartStreamTranscription"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["polly:SynthesizeSpeech"],
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
python3 transcribe.py /path/to/audio.opus

# 健康检查
curl http://localhost:8080/health
```

---

## 注意事项

1. **飞书语音气泡**：OpenClaw 原生支持，`VOICE_BUBBLE_CHANNELS` 包含 `feishu`，TTS 回复自动以语音气泡形式发出
2. **飞书音频格式**：飞书发送的语音为 opus 格式，FFmpeg 可直接处理
3. **Transcribe 区域**：`transcribe.py` 默认使用 `us-east-1`，可按需修改
4. **AWS 凭证**：建议在 EC2 上使用 IAM Role，无需配置 access key
5. **TTS 配置格式升级**：若从 v1 升级，把 `messages.tts.openai` 移到 `messages.tts.providers.openai`

---

## 技术栈

- **OpenClaw** — AI Agent 框架，处理消息路由、工具调用和语音气泡发送
- **AWS Transcribe Streaming** — 实时语音转文字
- **AWS Polly** — 神经网络 TTS（文字转语音）
- **FastAPI + uvicorn** — Polly Proxy 服务
- **FFmpeg** — 音频格式转换
- **飞书开放平台** — 消息收发和文件管理

---

## Changelog

### v2.0.0 (2026-04-18) — production sync
- 目录扁平化：根目录放 `polly_proxy.py` / `transcribe.py` / `requirements.txt`
- 移除 `polly-proxy/`、`scripts/`、`requirements-polly.txt`、`requirements-transcribe.txt`
- README/systemd unit/路径全部对齐生产环境（`/home/ubuntu/transcribe_polly/`）
- TTS 配置改为新格式 `messages.tts.providers.openai`
- 补充 `tools.media.audio.echoFormat` 示例

### v1.0.0
- 初始版本，`polly-proxy/main.py` + `scripts/transcribe.py` 分家布局
