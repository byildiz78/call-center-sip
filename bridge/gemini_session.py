"""Gemini Live API WebSocket session manager.

Each phone call gets its own GeminiSession instance with an independent
WebSocket connection to the Gemini Live API.
"""

import json
import logging
from dataclasses import dataclass
from typing import AsyncGenerator

import websockets

from bridge.config import (
    GEMINI_WS_URL, GEMINI_MODEL, GEMINI_VOICE,
    get_system_prompt, get_greeting, get_settings,
)

logger = logging.getLogger(__name__)


@dataclass
class AudioChunk:
    """A chunk of audio or text received from Gemini."""
    audio_b64: str = ""
    text: str = ""
    role: str = "assistant"
    turn_complete: bool = False
    end_call: bool = False  # True when agent says the end phrase


class GeminiSession:
    """Manages a single Gemini Live API WebSocket session."""

    def __init__(self, call_sid: str, input_sample_rate: int = 16000):
        self.call_sid = call_sid
        self.input_sample_rate = input_sample_rate
        self.ws = None
        self.transcript: list[dict] = []
        self._connected = False
        self._agent_text_buf: str = ""
        self._user_text_buf: str = ""
        self._end_call_detected: bool = False

    async def connect(self):
        """Open WebSocket connection and send the setup message."""
        greeting = get_greeting()

        # Build system prompt with greeting instruction
        prompt = get_system_prompt()
        full_prompt = (
            f"{prompt}\n\n"
            f"## First Message\n"
            f"As soon as the session starts, immediately say exactly this greeting "
            f"without waiting for any input:\n"
            f'"{greeting}"'
        )

        self.ws = await websockets.connect(
            GEMINI_WS_URL,
            max_size=None,
            ping_interval=30,
            ping_timeout=10,
        )

        setup_msg = {
            "setup": {
                "model": GEMINI_MODEL,
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE}
                        },
                        "languageCode": "tr-TR",
                    },
                },
                "systemInstruction": {
                    "parts": [{"text": full_prompt}]
                },
                "outputAudioTranscription": {},
                "inputAudioTranscription": {},
            }
        }
        await self.ws.send(json.dumps(setup_msg))

        # Wait for setup acknowledgement
        setup_resp = await self.ws.recv()
        setup_data = json.loads(setup_resp)
        logger.info("Gemini session ready for call %s: %s",
                     self.call_sid, str(setup_data)[:150])
        self._connected = True

        # Trigger greeting by sending silence so Gemini starts speaking
        import base64
        silence_bytes = self.input_sample_rate  # ~500ms silence
        silence = base64.b64encode(b"\x00" * silence_bytes).decode("ascii")
        await self.send_audio(silence, sample_rate=self.input_sample_rate)

    async def send_audio(self, pcm_b64: str, sample_rate: int = 16000):
        """Send a base64-encoded PCM audio chunk to Gemini."""
        if not self.ws or not self._connected:
            return
        msg = {
            "realtimeInput": {
                "mediaChunks": [
                    {"mimeType": f"audio/pcm;rate={sample_rate}", "data": pcm_b64}
                ]
            }
        }
        await self.ws.send(json.dumps(msg))

    @staticmethod
    def _normalize_turkish(text: str) -> str:
        """Normalize Turkish text for case-insensitive comparison."""
        return (
            text.lower()
            .replace("İ", "i").replace("I", "ı")
            .replace("\u0130", "i")   # İ (U+0130) → i
            .replace("\u0307", "")    # combining dot above
            .replace("ı", "i")       # dotless ı → i for matching
        )

    def _check_end_call(self, agent_text: str) -> bool:
        """Check if the call should end based on agent's spoken text."""
        if self._end_call_detected:
            return True
        end_phrase = get_settings().get("end_call_phrase", "iyi günler dilerim")
        norm_text = self._normalize_turkish(agent_text)
        norm_phrase = self._normalize_turkish(end_phrase)
        if norm_phrase and norm_phrase in norm_text:
            self._end_call_detected = True
            logger.info("End phrase detected in call %s", self.call_sid)
            return True
        return False

    async def receive(self) -> AsyncGenerator[AudioChunk, None]:
        """Yield AudioChunk objects from the Gemini stream.

        Transcription deltas are accumulated and flushed as complete
        messages when the turn completes or the speaker changes.
        """
        if not self.ws:
            return

        try:
            async for message in self.ws:
                data = json.loads(message)
                server_content = data.get("serverContent")
                if not server_content:
                    continue

                model_turn = server_content.get("modelTurn")
                if model_turn and "parts" in model_turn:
                    for part in model_turn["parts"]:
                        if "inlineData" in part:
                            yield AudioChunk(
                                audio_b64=part["inlineData"].get("data", "")
                            )
                        elif "text" in part:
                            thinking = part["text"]
                            if "{{end call}}" in thinking.lower():
                                self._end_call_detected = True

                # Agent speech transcription (accumulate deltas)
                output_transcription = server_content.get("outputTranscription")
                if output_transcription and output_transcription.get("text"):
                    if self._user_text_buf:
                        user_text = self._user_text_buf.strip()
                        if user_text:
                            self.transcript.append({"role": "user", "text": user_text})
                            yield AudioChunk(text=user_text, role="user")
                        self._user_text_buf = ""
                    self._agent_text_buf += output_transcription["text"]

                # Caller speech transcription (accumulate deltas)
                input_transcription = server_content.get("inputTranscription")
                if input_transcription and input_transcription.get("text"):
                    agent_text = self._agent_text_buf.strip()
                    if agent_text:
                        self.transcript.append({"role": "assistant", "text": agent_text})
                        self._check_end_call(agent_text)
                        yield AudioChunk(text=agent_text, role="assistant")
                    self._agent_text_buf = ""
                    self._user_text_buf += input_transcription["text"]

                if server_content.get("turnComplete"):
                    # Flush agent buf
                    agent_text = self._agent_text_buf.strip()
                    if agent_text:
                        self.transcript.append({"role": "assistant", "text": agent_text})
                        self._check_end_call(agent_text)
                        yield AudioChunk(text=agent_text, role="assistant")
                    self._agent_text_buf = ""
                    # Flush user buf
                    user_text = self._user_text_buf.strip()
                    if user_text:
                        self.transcript.append({"role": "user", "text": user_text})
                        yield AudioChunk(text=user_text, role="user")
                    self._user_text_buf = ""
                    # Also check cumulative transcript for end phrase
                    if not self._end_call_detected:
                        all_agent = " ".join(
                            t["text"] for t in self.transcript if t["role"] == "assistant"
                        )
                        self._check_end_call(all_agent)
                    yield AudioChunk(
                        turn_complete=True,
                        end_call=self._end_call_detected,
                    )

        except websockets.exceptions.ConnectionClosed:
            self._flush_remaining()
            logger.info("Gemini connection closed for call %s", self.call_sid)

    def _flush_remaining(self):
        text = self._agent_text_buf.strip()
        if text:
            self.transcript.append({"role": "assistant", "text": text})
        self._agent_text_buf = ""
        text = self._user_text_buf.strip()
        if text:
            self.transcript.append({"role": "user", "text": text})
        self._user_text_buf = ""

    async def close(self):
        """Close the Gemini WebSocket connection."""
        self._connected = False
        self._flush_remaining()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    def get_caller_info(self) -> dict:
        """Extract caller name and business name from transcript.

        Assumes the conversation flow: greeting → name → business name → problem.
        """
        user_messages = [t["text"] for t in self.transcript if t["role"] == "user"]
        caller_name = user_messages[0].strip().rstrip(".") if len(user_messages) > 0 else ""
        business_name = user_messages[1].strip().rstrip(".") if len(user_messages) > 1 else ""
        return {"caller_name": caller_name, "business_name": business_name}

    def get_issue_summary(self) -> str:
        """Extract only the user's problem/issue from the transcript."""
        user_messages = [t["text"] for t in self.transcript if t["role"] == "user"]
        if not user_messages:
            return ""
        # Skip name (1st) and business (2nd), rest is the problem
        problem_parts = user_messages[2:] if len(user_messages) > 2 else user_messages
        return " ".join(problem_parts).strip()[:500]

    def get_sentiment(self) -> str:
        """Analyze basic sentiment from user messages."""
        user_text = " ".join(
            t["text"].lower() for t in self.transcript if t["role"] == "user"
        )
        if not user_text:
            return "notr"

        negative_words = [
            "sorun", "problem", "çalışmıyor", "bozuk", "hata", "arıza",
            "kötü", "berbat", "rezalet", "şikayet", "memnun değil",
            "olmaz", "yapamıyorum", "gelmiyor", "açılmıyor", "kapanıyor",
            "donuyor", "kasıyor", "yavaş", "kopuyor", "kesildi",
        ]
        positive_words = [
            "teşekkür", "sağol", "memnun", "güzel", "harika", "süper",
            "iyi", "mükemmel", "tebrik", "başarılı",
        ]

        neg_count = sum(1 for w in negative_words if w in user_text)
        pos_count = sum(1 for w in positive_words if w in user_text)

        if neg_count > pos_count:
            return "negatif"
        elif pos_count > neg_count:
            return "pozitif"
        return "notr"
