"""
Amazon Polly TTS Proxy
兼容 OpenAI TTS API 格式  POST /v1/audio/speech → audio/mpeg
自动把合成语音发到飞书语音气泡
"""
import asyncio
import io
import logging
import os
import subprocess
import tempfile
import time

import boto3
import httpx
import uvicorn
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Polly TTS Proxy", version="2.0.0")

# 飞书配置 - 从环境变量读取
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "your_app_id_here")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "your_app_secret_here")
FEISHU_DEFAULT_CHAT_ID = os.getenv("FEISHU_DEFAULT_CHAT_ID", "")
MAX_TTS_CHARS = 200

_feishu_token_cache = {"token": None, "expires_at": 0}


async def get_feishu_token() -> str:
    now = time.time()
    if _feishu_token_cache["token"] and _feishu_token_cache["expires_at"] > now + 60:
        return _feishu_token_cache["token"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        )
        data = resp.json()
        token = data.get("tenant_access_token")
        expire = data.get("expire", 7200)
        _feishu_token_cache["token"] = token
        _feishu_token_cache["expires_at"] = now + expire
        return token


async def send_feishu_audio(mp3_bytes: bytes, chat_id: str, receive_type: str = "chat_id"):
    """后台异步：MP3 → opus → 上传飞书 → 发语音消息"""
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as mp3f:
            mp3f.write(mp3_bytes)
            mp3_path = mp3f.name
        opus_path = mp3_path.replace(".mp3", ".opus")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path, "-c:a", "libopus", "-ar", "16000", "-ac", "1", opus_path],
            capture_output=True, timeout=30
        )
        if result.returncode != 0:
            logger.error("ffmpeg failed: %s", result.stderr.decode())
            return

        token = await get_feishu_token()
        headers = {"Authorization": f"Bearer {token}"}

        with open(opus_path, "rb") as f:
            opus_data = f.read()

        async with httpx.AsyncClient(timeout=30) as client:
            upload_resp = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/files",
                headers=headers,
                data={"file_type": "opus", "file_name": "voice.opus"},
                files={"file": ("voice.opus", opus_data, "audio/ogg")},
            )
            upload_data = upload_resp.json()
            if upload_data.get("code") != 0:
                logger.error("Feishu upload failed: %s", upload_data)
                return

            file_key = upload_data["data"]["file_key"]

            import json
            await client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                headers={**headers, "Content-Type": "application/json"},
                params={"receive_id_type": receive_type},
                json={
                    "receive_id": chat_id,
                    "msg_type": "audio",
                    "content": json.dumps({"file_key": file_key}),
                },
            )

        os.unlink(mp3_path)
        os.unlink(opus_path)

    except Exception as e:
        logger.error("send_feishu_audio error: %s", e, exc_info=True)


class TTSRequest(BaseModel):
    model: str = Field(default="polly")
    input: str = Field(..., description="Text to synthesize")
    voice: str = Field(default="Zhiyu", description="Polly VoiceId")
    feishu_chat_id: Optional[str] = Field(default=None)
    feishu_receive_type: Optional[str] = Field(default="chat_id")


@app.post("/v1/audio/speech")
async def synthesize_speech(request: TTSRequest):
    try:
        polly = boto3.client("polly")
        resp = polly.synthesize_speech(
            VoiceId=request.voice,
            Engine="neural",
            OutputFormat="mp3",
            Text=request.input,
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg  = e.response["Error"]["Message"]
        raise HTTPException(status_code=400, detail=f"Polly [{code}]: {msg}")
    except BotoCoreError as e:
        raise HTTPException(status_code=500, detail=f"AWS error: {e}")

    audio_bytes = resp["AudioStream"].read()

    if len(request.input) <= MAX_TTS_CHARS:
        chat_id = request.feishu_chat_id or FEISHU_DEFAULT_CHAT_ID
        receive_type = request.feishu_receive_type or "chat_id"
        asyncio.create_task(send_feishu_audio(audio_bytes, chat_id, receive_type))

    return StreamingResponse(
        io.BytesIO(audio_bytes),
        media_type="audio/mpeg",
        headers={"Content-Length": str(len(audio_bytes)), "X-Voice-Id": request.voice},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
