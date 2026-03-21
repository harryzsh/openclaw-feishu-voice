# OpenClaw 飞书语音收发架构

> 基于 **OpenClaw + AWS Transcribe Streaming + AWS Polly** 实现飞书机器人双向语音能力。
> 用户发语音 → AI 识别理解 → 语音回复，全程自动，零人工干预。

---

## 整体架构

```
[用户发飞书语音消息]
        │
        ▼
[飞书 Webhook Event: im.message.receive_v1]
        │  audio 消息，含 file_key
        ▼
[OpenClaw 框架下载音频（opus 格式）]
        │
        ▼
┌─────────────────────────────┐
│  scripts/transcribe.py       │
│  ffmpeg: opus → PCM 16kHz   │  ← 内存 pipe，不落盘
│  AWS Transcribe Streaming   │  ← 实时流式识别
└─────────────────────────────┘
        │ 识别文字
        ▼
[OpenClaw Agent（Claude on Bedrock）]
        │ 生成文字回复
        ▼
[hooks/polly-voice-reply/handler.ts]
        │ 监听 agent 输出，触发 TTS
        ▼
┌─────────────────────────────┐
│  polly-proxy/main.py         │
│  POST /v1/audio/speech       │  ← 兼容 OpenAI TTS API
│  AWS Polly Neural (Zhiyu)   │  ← 中文 Neural 语音合成
│  MP3 → opus（ffmpeg）       │
│  飞书 API 上传 + 发语音消息  │
└─────────────────────────────┘
        │
        ▼
[用户收到飞书语音气泡 🎵]
```

---

## 目录结构

```
openclaw-feishu-voice/
├── README.md
├── scripts/
│   └── transcribe.py              # 语音转文字（AWS Transcribe Streaming）
├── polly-proxy/
│   ├── main.py                    # 文字转语音服务（AWS Polly）
│   └── requirements.txt
├── hooks/
│   └── polly-voice-reply/
│       └── handler.ts             # OpenClaw Hook，触发 TTS
└── config/
    └── openclaw-audio-tts.json    # OpenClaw 配置示例（脱敏）
```

---

## 三个核心组件详解

### 1. `scripts/transcribe.py` — 语音转文字

**职责**：OpenClaw 收到飞书音频后，调用此脚本把 opus 文件转为文字。

**技术选型**：
- 使用 **AWS Transcribe Streaming**（实时流式 API），而非批处理模式
- 无需 S3，音频通过 **内存 pipe** 直接传给 Transcribe，延迟更低
- ffmpeg 负责格式转换：`opus → PCM 16kHz 单声道`（Transcribe 要求格式）

**数据安全**：
- 音频数据全程在内存中处理，**从不写磁盘**
- Transcribe Streaming 处理完后即释放，无任何持久化

**调用方式（由 OpenClaw 自动调用）**：
```bash
python3 transcribe.py /tmp/audio_xyz.opus --lang zh-CN
# stdout 输出识别文字，供 OpenClaw 使用
```

---

### 2. `polly-proxy/main.py` — 文字转语音代理服务

**职责**：常驻 HTTP 服务，接收文字，合成语音，自动发回飞书。

**接口设计**：
- 完全兼容 **OpenAI TTS API** 格式（`POST /v1/audio/speech`）
- OpenClaw 把它当作 OpenAI TTS 使用，**框架层无需任何修改**

**核心流程**：
1. 接收 `{ "input": "文字内容", "voice": "Zhiyu" }`
2. 调用 AWS Polly Neural 合成 MP3
3. ffmpeg 转码：MP3 → opus（飞书原生语音格式）
4. 调飞书 API 上传文件，获取 `file_key`
5. 发送 `audio` 类型飞书消息
6. **删除临时文件**（`/tmp/*.mp3`, `/tmp/*.opus`）

**关键配置**：
- 语音：`Zhiyu`（普通话，Neural 引擎，目前唯一中文 Neural 声音）
- 字数限制：超过 200 字不触发语音（避免过长等待）
- 飞书 token 自动刷新，缓存 2 小时

---

### 3. `hooks/polly-voice-reply/handler.ts` — OpenClaw Hook

**职责**：作为 OpenClaw 内部钩子，监听每条 agent 输出消息，自动触发 TTS。

**触发规则**：
| 条件 | 行为 |
|------|------|
| 飞书频道 + ≤200 字 | ✅ 触发 Polly 语音 |
| 非飞书频道 | ❌ 跳过 |
| >200 字 | ❌ 跳过 |
| `NO_REPLY` / `HEARTBEAT_OK` | ❌ 跳过（系统消息） |

---

## 前提条件

### AWS 权限（IAM Policy）

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

> 如果运行在 EC2/ECS，推荐用 **Instance Role / Task Role**，无需配置 Access Key。

### 系统依赖

```bash
# ffmpeg（必须）
sudo apt-get install -y ffmpeg

# Python 依赖（transcribe.py）
pip install amazon-transcribe boto3

# Python 依赖（polly-proxy）
pip install fastapi uvicorn boto3 httpx pydantic
```

### 飞书应用权限

在 [飞书开发者后台](https://open.feishu.cn/app) 开启以下权限：

| 权限 | 用途 |
|------|------|
| `im:message` | 发送消息 |
| `im:message:send_as_bot` | 以机器人身份发消息 |
| `im:resource` | 上传/下载文件（**语音收发必须**） |
| `im:message.group_msg` | 群聊消息接收 |

---

## 部署步骤

### Step 1：克隆代码

```bash
git clone https://github.com/harryzsh/openclaw-feishu-voice
cd openclaw-feishu-voice
```

### Step 2：部署 polly-proxy

```bash
cd polly-proxy
pip install -r requirements.txt

# 配置环境变量（替换为你的飞书 App 信息）
export FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
export FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 启动服务
python3 main.py
# 服务运行在 http://localhost:8080
```

验证服务正常：
```bash
curl http://localhost:8080/health
# 返回：{"status":"ok"}
```

**配置 systemd 开机自启（推荐生产环境）**：

```ini
# /etc/systemd/system/polly-proxy.service
[Unit]
Description=Polly TTS Proxy for OpenClaw
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/polly-proxy
Environment=FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
Environment=FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable polly-proxy
sudo systemctl start polly-proxy
```

### Step 3：部署 transcribe.py

```bash
# 安装依赖
pip install amazon-transcribe boto3

# 复制到 OpenClaw 工作目录
mkdir -p ~/clawd/scripts
cp scripts/transcribe.py ~/clawd/scripts/transcribe.py
```

测试：
```bash
python3 ~/clawd/scripts/transcribe.py test.opus --lang zh-CN
# 应输出识别文字
```

### Step 4：配置 OpenClaw

编辑 `~/.openclaw/openclaw.json`，在 `tools` 和 `messages` 下添加：

```json
{
  "tools": {
    "media": {
      "audio": {
        "enabled": true,
        "echoTranscript": false,
        "models": [
          {
            "type": "cli",
            "command": "python3",
            "args": [
              "/home/ubuntu/clawd/scripts/transcribe.py",
              "{{MediaPath}}"
            ],
            "timeoutSeconds": 30
          }
        ]
      }
    }
  },
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
}
```

> **关键点**：`provider: "openai"` + 自定义 `baseUrl` 指向本地 polly-proxy，
> OpenClaw 会把它当作 OpenAI TTS 调用，完全透明。

### Step 5：部署 OpenClaw Hook

```bash
# 创建 hook 目录
mkdir -p ~/.openclaw/workspace/hooks/polly-voice-reply

# 复制 hook 文件
cp hooks/polly-voice-reply/handler.ts \
   ~/.openclaw/workspace/hooks/polly-voice-reply/
```

在 `openclaw.json` 的 `hooks` 部分添加：

```json
{
  "hooks": {
    "enabled": true,
    "internal": {
      "enabled": true,
      "entries": {
        "polly-voice-reply": { "enabled": true }
      },
      "load": {
        "extraDirs": [
          "/home/ubuntu/.openclaw/workspace/hooks"
        ]
      }
    }
  }
}
```

### Step 6：重启 OpenClaw

```bash
openclaw gateway restart
```

### Step 7：验证

1. 在飞书向机器人发一条**语音消息**
2. 机器人应识别语音内容并回复文字
3. 机器人的文字回复（≤200字）会自动触发，发回**语音气泡** 🎵

---

## 数据流与隐私

| 数据 | 存储位置 | 生命周期 |
|------|---------|---------|
| 用户语音（输入） | 内存（不落盘） | 请求结束即释放 |
| Polly 生成 MP3 | `/tmp/` | 上传飞书后立即删除 |
| Opus 转码文件 | `/tmp/` | 上传飞书后立即删除 |
| 飞书服务器 | 飞书云存储 | 飞书自己管理 |

**服务端零音频留存**，符合数据最小化原则。

---

## 延迟参考

| 步骤 | 典型耗时 |
|------|---------|
| 飞书音频下载 | ~0.5s |
| ffmpeg 格式转换 | ~0.3s |
| AWS Transcribe Streaming | ~3–8s |
| AI 生成回复（Bedrock） | ~1–2s |
| AWS Polly 合成 | ~0.5–1s |
| ffmpeg MP3 → opus | ~0.2s |
| 飞书上传 + 发送 | ~0.5s |
| **总端到端** | **~6–12s** |

---

## AWS 费用估算

| 服务 | 单价 | 每 100 条语音消息 |
|------|------|----------------|
| Transcribe Streaming | $0.024/分钟 | ~$0.24（按 10 秒/条） |
| Polly Neural | $0.016/千字符 | ~$0.08（按 50 字/条） |
| **合计** | — | **~$0.32** |

> 对于日常对话量，月费用通常在 **$5 以内**。

---

## 常见问题

**Q: 为什么用 Transcribe Streaming 而不是批处理？**
> 批处理需要先上传 S3，有额外延迟和存储费用。Streaming 直接流式推送，无需 S3，延迟低 2–5 秒。

**Q: Polly 为什么选 Zhiyu Neural？**
> Zhiyu 是目前 AWS Polly 唯一支持 Neural 引擎的中文（普通话）声音，音质更自然。Standard 引擎的 Zhiyu 也可用，但音质略差。

**Q: 为什么 TTS Provider 配置为 "openai"？**
> polly-proxy 实现了完全兼容 OpenAI TTS API 的接口，OpenClaw 内置支持 OpenAI TTS，通过 `baseUrl` 重定向到本地服务，无需修改框架任何代码。

**Q: 音频文件会被保存吗？**
> 不会。输入音频全程内存处理；输出音频临时存 `/tmp/`，上传飞书后立即删除。

---

## License

MIT
