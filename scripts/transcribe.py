#!/usr/bin/env python3
"""
Audio transcription using AWS Transcribe Streaming.
Usage: python3 transcribe.py <audio_file> [--lang zh-CN]
Outputs transcribed text to stdout.
"""
import asyncio, sys, os
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent
import subprocess

class Handler(TranscriptResultStreamHandler):
    def __init__(self, stream):
        super().__init__(stream)
        self.transcripts = []

    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        results = transcript_event.transcript.results
        for result in results:
            if not result.is_partial:
                for alt in result.alternatives:
                    self.transcripts.append(alt.transcript)

async def transcribe(audio_path, lang="zh-CN"):
    cmd = [
        "ffmpeg", "-i", audio_path,
        "-ar", "16000", "-ac", "1", "-f", "s16le",
        "-acodec", "pcm_s16le", "-y", "pipe:1"
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        print(f"ffmpeg error: {proc.stderr.decode()}", file=sys.stderr)
        sys.exit(1)
    pcm_data = proc.stdout

    client = TranscribeStreamingClient(region="us-east-1")
    stream = await client.start_stream_transcription(
        language_code=lang,
        media_sample_rate_hz=16000,
        media_encoding="pcm",
    )

    handler = Handler(stream.output_stream)

    chunk_size = 16000 * 2
    async def send_audio():
        for i in range(0, len(pcm_data), chunk_size):
            chunk = pcm_data[i:i + chunk_size]
            await stream.input_stream.send_audio_event(audio_chunk=chunk)
        await stream.input_stream.end_stream()

    await asyncio.gather(send_audio(), handler.handle_events())
    return " ".join(handler.transcripts)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: transcribe.py <audio_file> [--lang zh-CN]", file=sys.stderr)
        sys.exit(1)

    audio_path = sys.argv[1]
    lang = "zh-CN"
    if "--lang" in sys.argv:
        idx = sys.argv.index("--lang")
        lang = sys.argv[idx + 1]

    if not os.path.exists(audio_path):
        print(f"File not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    text = asyncio.run(transcribe(audio_path, lang))
    print(text)
