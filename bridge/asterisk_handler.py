"""Asterisk AudioSocket handler.

Asterisk sends raw audio via TCP (AudioSocket protocol):
- 16-bit signed linear PCM at 8kHz
- Bidirectional: we receive caller audio and send back agent audio

Dialplan: same => n,AudioSocket(127.0.0.1:9093)

Protocol:
- Each frame: 1 byte type + 3 bytes length (big-endian) + payload
- Type 0x01 = UUID (first message, 36 bytes)
- Type 0x10 = Audio (raw PCM slin, 8kHz 16-bit mono)
- Type 0x02 = Silence
- Type 0xFF = Hangup
"""

import asyncio
import base64
import logging
import os
import struct
import time
import uuid

from bridge.gemini_session import GeminiSession
from bridge.recorder import CallRecorder
from bridge.audio_utils import resample
from bridge.config import get_settings, PUBLIC_URL, BRIDGE_PORT
from bridge import db

logger = logging.getLogger(__name__)

AUDIOSOCKET_PORT = 9093


async def get_caller_number() -> str:
    """Get caller number from temp file written by Asterisk dialplan."""
    import glob
    try:
        files = sorted(glob.glob("/tmp/last_caller_*.txt"), key=os.path.getmtime, reverse=True)
        if files:
            with open(files[0], "r") as f:
                num = f.read().strip()
                if num:
                    return num
    except Exception as e:
        logger.warning("Failed to read caller number: %s", e)
    return "unknown"

# AudioSocket frame types
TYPE_UUID = 0x01
TYPE_SILENCE = 0x02
TYPE_AUDIO = 0x10
TYPE_ERROR = 0xFF


async def read_frame(reader: asyncio.StreamReader):
    """Read one AudioSocket frame. Returns (type, payload).

    Asterisk AudioSocket frame format:
    - 1 byte: type
    - 2 bytes: length (network byte order, big-endian)
    - N bytes: payload
    """
    header = await reader.readexactly(3)
    frame_type = header[0]
    length = (header[1] << 8) | header[2]
    payload = await reader.readexactly(length) if length > 0 else b""
    return frame_type, payload


def make_frame(frame_type: int, payload: bytes) -> bytes:
    """Build an AudioSocket frame."""
    length = len(payload)
    header = bytes([frame_type, (length >> 8) & 0xFF, length & 0xFF])
    return header + payload


async def handle_audiosocket(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle one AudioSocket connection (one call)."""
    peer = writer.get_extra_info("peername")
    logger.info("AudioSocket connection from %s", peer)

    try:
        frame_type, payload = await asyncio.wait_for(read_frame(reader), timeout=5)
        logger.info("First frame: type=0x%02x len=%d", frame_type, len(payload))

        if frame_type != TYPE_UUID:
            logger.error("Expected UUID (0x01), got 0x%02x", frame_type)
            writer.close()
            return

        call_uuid = payload.decode("utf-8", errors="ignore").strip()
        call_sid = f"sip-{int(time.time())}"
        logger.info("AudioSocket call started: %s (uuid: %s)", call_sid, call_uuid)
    except Exception as e:
        logger.error("Error reading first frame: %s", e)
        writer.close()
        return

    start_time = time.time()
    recorder = CallRecorder(call_sid, caller_rate=8000)
    gemini = GeminiSession(call_sid, input_sample_rate=16000)  # Resample 8k→16k with soxr
    settings = get_settings()
    max_duration = settings.get("max_call_duration", 90)

    # Get caller number from file written by Asterisk dialplan
    caller_number = await get_caller_number()
    logger.info("Caller number: %s", caller_number)

    await db.create_call(call_sid, caller_number)

    try:
        await gemini.connect()
        logger.info("Gemini connected for %s", call_sid)
    except Exception as e:
        logger.error("Gemini connect failed for %s: %s", call_sid, e)
        await db.fail_call(call_sid, str(e))
        writer.close()
        return

    call_ended = asyncio.Event()

    audio_frame_count = 0
    # Asterisk AudioSocket sends slin 8kHz: 320 bytes per 20ms frame
    # Buffer ~100ms = 5 frames = 1600 bytes
    BUFFER_SIZE = 1600

    async def asterisk_to_gemini():
        """Read 8kHz audio from Asterisk, resample to 16kHz with soxr, send to Gemini."""
        nonlocal audio_frame_count
        audio_buffer = bytearray()
        try:
            while not call_ended.is_set():
                frame_type, payload = await read_frame(reader)
                if frame_type == TYPE_AUDIO and payload:
                    audio_frame_count += 1
                    if audio_frame_count == 1:
                        logger.info("First audio frame: %d bytes (call %s)", len(payload), call_sid)
                    audio_buffer.extend(payload)
                    if len(audio_buffer) >= BUFFER_SIZE:
                        chunk = bytes(audio_buffer)
                        audio_buffer.clear()
                        recorder.write_caller(chunk)
                        # Resample 8kHz → 16kHz with soxr HQ
                        pcm_16k = resample(chunk, 8000, 16000)
                        b64 = base64.b64encode(pcm_16k).decode("ascii")
                        await gemini.send_audio(b64, sample_rate=16000)
                elif frame_type == TYPE_ERROR:
                    logger.info("Error frame received for %s", call_sid)
                    break
                elif frame_type == TYPE_SILENCE or frame_type == TYPE_UUID:
                    pass
            if audio_buffer:
                chunk = bytes(audio_buffer)
                recorder.write_caller(chunk)
                pcm_16k = resample(chunk, 8000, 16000)
                b64 = base64.b64encode(pcm_16k).decode("ascii")
                await gemini.send_audio(b64, sample_rate=16000)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            logger.info("Asterisk disconnected for %s", call_sid)
        except Exception as e:
            logger.error("asterisk_to_gemini error [%s]: %s", call_sid, e)
        finally:
            call_ended.set()

    async def gemini_to_asterisk():
        """Read audio from Gemini, buffer, resample 24kHz → 8kHz, pace at 20ms."""
        FRAME_SIZE = 320  # 20ms @ 8kHz 16-bit mono
        FRAME_DURATION = 0.02  # 20ms
        PREBUFFER_MS = 300  # Buffer 300ms before starting playback
        PREBUFFER_BYTES = int(8000 * 2 * PREBUFFER_MS / 1000)  # 4800 bytes

        pcm_buffer = bytearray()
        raw_24k_buffer = bytearray()  # Accumulate 24kHz before resampling
        RAW_FLUSH_SIZE = 4800  # ~100ms of 24kHz audio (24000*2*0.1)
        playback_started = False
        should_end = False

        async def receive_audio():
            """Receive from Gemini, batch-resample, fill playback buffer."""
            nonlocal playback_started, should_end
            try:
                async for chunk in gemini.receive():
                    if call_ended.is_set():
                        break
                    if chunk.audio_b64:
                        pcm_24k = base64.b64decode(chunk.audio_b64)
                        recorder.write_agent(pcm_24k)
                        raw_24k_buffer.extend(pcm_24k)
                        # Batch resample when enough accumulated
                        if len(raw_24k_buffer) >= RAW_FLUSH_SIZE:
                            pcm_8k = resample(bytes(raw_24k_buffer), 24000, 8000)
                            pcm_buffer.extend(pcm_8k)
                            raw_24k_buffer.clear()
                    if chunk.end_call:
                        logger.info("Agent end phrase for %s", call_sid)
                        should_end = True
                # Flush remaining 24k buffer
                if raw_24k_buffer:
                    pcm_8k = resample(bytes(raw_24k_buffer), 24000, 8000)
                    pcm_buffer.extend(pcm_8k)
                    raw_24k_buffer.clear()
            except Exception as e:
                logger.error("receive_audio error [%s]: %s", call_sid, e)

        async def send_audio():
            """Send buffered audio at precise 20ms intervals."""
            nonlocal playback_started
            try:
                # Wait for prebuffer to fill
                while len(pcm_buffer) < PREBUFFER_BYTES and not call_ended.is_set():
                    await asyncio.sleep(0.01)

                playback_started = True
                next_send = time.monotonic()

                while not call_ended.is_set():
                    if len(pcm_buffer) >= FRAME_SIZE:
                        frame_data = bytes(pcm_buffer[:FRAME_SIZE])
                        del pcm_buffer[:FRAME_SIZE]

                        writer.write(make_frame(TYPE_AUDIO, frame_data))
                        await writer.drain()

                        next_send += FRAME_DURATION
                        sleep_time = next_send - time.monotonic()
                        if sleep_time > 0:
                            await asyncio.sleep(sleep_time)
                    elif should_end:
                        # No more audio and agent said goodbye
                        await asyncio.sleep(0.5)
                        call_ended.set()
                        break
                    else:
                        # Buffer underrun - wait for more data
                        await asyncio.sleep(0.005)
                        next_send = time.monotonic()
            except (ConnectionResetError, BrokenPipeError):
                pass

        try:
            await asyncio.gather(receive_audio(), send_audio())
        except Exception as e:
            logger.error("gemini_to_asterisk error [%s]: %s", call_sid, e)
        finally:
            call_ended.set()

    async def timeout_watcher():
        await asyncio.sleep(max_duration)
        if not call_ended.is_set():
            logger.info("Max duration %ds for %s", max_duration, call_sid)
            call_ended.set()

    done, pending = await asyncio.wait(
        [
            asyncio.create_task(asterisk_to_gemini()),
            asyncio.create_task(gemini_to_asterisk()),
            asyncio.create_task(timeout_watcher()),
        ],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # Cleanup
    writer.close()
    await gemini.close()

    duration = int(time.time() - start_time)
    recording_path = recorder.finalize()
    transcript = gemini.transcript
    summary = gemini.get_issue_summary()
    sentiment = gemini.get_sentiment()
    caller_info = gemini.get_caller_info()

    await db.end_call(call_sid, duration, transcript, summary, recording_path, sentiment)
    logger.info("SIP call ended: %s | %ds | %s", call_sid, duration, sentiment)

    from bridge.ticket import send_webhook
    base_url = PUBLIC_URL or f"http://localhost:{BRIDGE_PORT}"
    recording_public_url = f"{base_url}/api/recordings/{call_sid}"
    asyncio.create_task(send_webhook(
        call_sid=call_sid,
        caller_number=caller_number,
        caller_name=caller_info["caller_name"],
        business_name=caller_info["business_name"],
        duration=duration,
        summary=summary,
        sentiment=sentiment,
        recording_url=recording_public_url,
        start_time=str(start_time),
    ))


async def start_audiosocket_server():
    """Start the AudioSocket TCP server."""
    server = await asyncio.start_server(
        handle_audiosocket, "127.0.0.1", AUDIOSOCKET_PORT,
    )
    logger.info("AudioSocket server listening on 127.0.0.1:%d", AUDIOSOCKET_PORT)
    async with server:
        await server.serve_forever()
