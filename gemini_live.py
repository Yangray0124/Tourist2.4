"""One text/image turn through Gemini Live, returned as PCM for Discord playback."""

import asyncio
import base64
import io
import wave
from dataclasses import dataclass
from typing import Any

LIVE_MODEL = "gemini-3.1-flash-live-preview"
# Edit this Python constant and restart the bot to change its voice.
DEFAULT_LIVE_VOICE = "Fenrir"
LIVE_SAMPLE_RATE = 24000
LIVE_MAX_AUDIO_BYTES = 8 * 1024 * 1024


class LiveVoiceError(RuntimeError):
    """Sanitized failure: no API key or request URL in the exception message."""


@dataclass(frozen=True)
class LiveReply:
    pcm: bytes
    transcript: str


def is_voice_recipient(message: Any, voice: Any) -> bool:
    state = getattr(getattr(message, "author", None), "voice", None)
    channel = getattr(state, "channel", None)
    return channel is not None and channel.id == voice.channel.id


def pcm_to_wav(pcm: bytes) -> bytes:
    if not pcm or len(pcm) % 2:
        raise ValueError("Invalid 16-bit PCM audio")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(LIVE_SAMPLE_RATE)
        wav.writeframes(pcm)
    return output.getvalue()


async def collect_live_reply(session: Any) -> LiveReply:
    audio = bytearray()
    transcript: list[str] = []
    completed = False
    async for response in session.receive():
        content = response.server_content
        if content is None:
            continue
        if content.interrupted:
            raise ValueError("Live response interrupted")
        if content.output_transcription and content.output_transcription.text:
            transcript.append(content.output_transcription.text)
        if content.model_turn:
            for part in content.model_turn.parts or []:
                blob = part.inline_data
                if blob and blob.data:
                    if not (blob.mime_type or "").startswith("audio/pcm"):
                        raise ValueError("Unexpected Live audio format")
                    if len(audio) + len(blob.data) > LIVE_MAX_AUDIO_BYTES:
                        raise ValueError("Live audio exceeds limit")
                    audio.extend(blob.data)
        if content.turn_complete:
            completed = True
            break
    if not completed or not audio or len(audio) % 2:
        raise ValueError("Live did not return a complete audio answer")
    return LiveReply(bytes(audio), "".join(transcript).strip())


async def generate_live_reply(
    payload: dict[str, Any], api_key: str, voice_name: str = DEFAULT_LIVE_VOICE
) -> LiveReply:
    try:
        # Lazy import: ordinary text chat still works before this dependency is installed.
        from google import genai
        from google.genai import types

        instruction = "\n".join(
            part.get("text", "") for part in payload.get("systemInstruction", {}).get("parts", [])
        )
        config = {
            "response_modalities": ["AUDIO"],
            "output_audio_transcription": {},
            "system_instruction": instruction + (
                "\n請依使用者要求及之前對話的活動情境，自然地說話。"
                "語言依使用者指定，可說英文或其他語言；不要自行附帶翻譯。"
                "請維持偏男性、自然低沉且穩定的聲線，音高稍低，避免尖細或刻意提高音調，"
                "但保持發音清楚、語氣自然，不要刻意壓嗓。"
                "回答的完整程度應與文字聊天相同，依問題提供足夠的內容、解釋及必要步驟。"
                "在這一次語音回覆中把當前問題完整說完，不要只講開頭或一小段就停下來。"
                "不要用『要我繼續嗎』或不必要的反問，把本來可以直接回答的內容拆成多輪。"
                "只有缺少必要資訊，或使用者明確要求互動問答、逐題練習、英聽測驗時，"
                "才提出問題並等待回覆；若使用者要求簡短，則依要求縮短。"
            ),
            "temperature": payload.get("generationConfig", {}).get("temperature", 1.6),
            "thinking_config": {"thinking_level": "minimal"},
            "speech_config": {
                "voice_config": {"prebuilt_voice_config": {"voice_name": voice_name}},
            },
        }
        turns = []
        for turn in payload["contents"]:
            parts = []
            for part in turn["parts"]:
                if "text" in part:
                    parts.append(types.Part.from_text(text=part["text"]))
                elif "inline_data" in part:
                    blob = part["inline_data"]
                    parts.append(types.Part.from_bytes(
                        data=base64.b64decode(blob["data"], validate=True), mime_type=blob["mime_type"]
                    ))
            turns.append(types.Content(role=turn.get("role", "user"), parts=parts))
        async with asyncio.timeout(60):
            async with genai.Client(api_key=api_key).aio as client:
                async with client.live.connect(model=LIVE_MODEL, config=config) as session:
                    await session.send_client_content(turns=turns, turn_complete=True)
                    return await collect_live_reply(session)
    except Exception as error:
        code = getattr(error, "code", None)
        detail = f"HTTP {code}" if isinstance(code, int) else type(error).__name__
        raise LiveVoiceError(detail) from None
