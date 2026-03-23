"""
Amazon Polly TTS Proxy
兼容 OpenAI TTS API 格式  POST /v1/audio/speech → audio/mpeg

飞书语音气泡由 OpenClaw 原生处理（VOICE_BUBBLE_CHANNELS 包含 feishu），
本代理只负责 TTS 生成，不做任何发送逻辑。
"""
import io
import logging
import os

import boto3
import uvicorn
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Polly TTS Proxy", version="3.0.0")


class TTSRequest(BaseModel):
    model: Optional[str] = "polly"
    input: str
    voice: Optional[str] = "Zhiyu"
    response_format: Optional[str] = "mp3"
    speed: Optional[float] = None
    instructions: Optional[str] = None


@app.post("/v1/audio/speech")
async def tts(request: TTSRequest):
    voice = request.voice or "Zhiyu"
    text = request.input
    logger.info("TTS: voice=%s len=%d preview=%r", voice, len(text), text[:40])

    try:
        client = boto3.client("polly", region_name=os.getenv("AWS_REGION", "us-east-1"))
        response = client.synthesize_speech(
            Text=text,
            OutputFormat="mp3",
            VoiceId=voice,
            Engine="neural" if voice in ("Zhiyu",) else "standard",
        )
        audio_bytes = response["AudioStream"].read()
    except (BotoCoreError, ClientError) as e:
        logger.error("Polly error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return StreamingResponse(
        io.BytesIO(audio_bytes),
        media_type="audio/mpeg",
        headers={"Content-Disposition": "attachment; filename=speech.mp3"},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
