"""Call recording module - saves caller and agent audio as stereo WAV.

Caller audio (continuous mic stream) uses timestamps for placement.
Agent audio chunks are placed contiguously since they arrive in
network bursts faster than real-time. A gap threshold detects new
speaking turns and inserts silence accordingly.
"""

import os
import array
import time
import wave
from datetime import datetime
from bridge.audio_utils import resample
from bridge.config import RECORDINGS_DIR

SAMPLE_RATE = 16000
AGENT_NATIVE_RATE = 24000
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS_STEREO = 2

# If gap between agent chunks exceeds this, treat as a new speaking turn
TURN_GAP_SECONDS = 0.3


class CallRecorder:
    """Records a call as a stereo WAV file (caller=left, agent=right)."""

    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self._start_time: float = time.monotonic()
        self._caller_entries: list[tuple[float, bytes]] = []
        self._agent_entries: list[tuple[float, bytes]] = []
        self.filepath = ""

    def write_caller(self, pcm_16k: bytes):
        self._caller_entries.append((time.monotonic(), pcm_16k))

    def write_agent(self, pcm_24k: bytes):
        self._agent_entries.append((time.monotonic(), pcm_24k))

    def finalize(self) -> str:
        date_dir = datetime.utcnow().strftime("%Y-%m-%d")
        out_dir = os.path.join(RECORDINGS_DIR, date_dir)
        os.makedirs(out_dir, exist_ok=True)

        ts = datetime.utcnow().strftime("%H%M%S")
        self.filepath = os.path.join(out_dir, f"{self.call_sid}_{ts}.wav")

        if not self._caller_entries and not self._agent_entries:
            with wave.open(self.filepath, "wb") as wf:
                wf.setnchannels(CHANNELS_STEREO)
                wf.setsampwidth(SAMPLE_WIDTH)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(b"")
            return self.filepath

        # --- Determine total duration from caller stream ---
        total_duration = 0.0
        if self._caller_entries:
            last_ts, last_pcm = self._caller_entries[-1]
            total_duration = (last_ts - self._start_time) + len(last_pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)

        total_samples_16k = int(total_duration * SAMPLE_RATE) + SAMPLE_RATE
        total_samples_24k = int(total_duration * AGENT_NATIVE_RATE) + AGENT_NATIVE_RATE

        # --- Left channel: caller at 16kHz (continuous, timestamp-based) ---
        left = array.array("h", [0] * total_samples_16k)
        for ts_val, pcm in self._caller_entries:
            offset = int((ts_val - self._start_time) * SAMPLE_RATE)
            chunk = array.array("h")
            chunk.frombytes(pcm)
            for i, s in enumerate(chunk):
                pos = offset + i
                if 0 <= pos < total_samples_16k:
                    left[pos] = s

        # --- Right channel: agent at 24kHz (contiguous within turns) ---
        # Agent chunks arrive in bursts. Place them contiguously,
        # only inserting a time-based gap when a new turn starts.
        right_24k = array.array("h", [0] * total_samples_24k)
        agent_write_pos = 0  # next sample position to write at (24kHz)
        prev_arrival = None

        for ts_val, pcm_24k in self._agent_entries:
            chunk = array.array("h")
            chunk.frombytes(pcm_24k)
            chunk_samples = len(chunk)

            if prev_arrival is not None:
                gap = ts_val - prev_arrival
                if gap > TURN_GAP_SECONDS:
                    # New speaking turn - jump to timestamp-based position
                    ts_pos = int((ts_val - self._start_time) * AGENT_NATIVE_RATE)
                    if ts_pos > agent_write_pos:
                        agent_write_pos = ts_pos

            # Place chunk contiguously
            for i, s in enumerate(chunk):
                pos = agent_write_pos + i
                if 0 <= pos < total_samples_24k:
                    right_24k[pos] = s

            agent_write_pos += chunk_samples
            prev_arrival = ts_val

        # Single resample of the entire agent channel 24kHz → 16kHz
        right_16k_bytes = resample(right_24k.tobytes(), AGENT_NATIVE_RATE, SAMPLE_RATE)
        right = array.array("h")
        right.frombytes(right_16k_bytes)

        # Match channel lengths
        final_len = min(len(left), len(right))

        # Interleave into stereo
        stereo = array.array("h", [0] * (final_len * 2))
        for i in range(final_len):
            stereo[i * 2] = left[i]
            stereo[i * 2 + 1] = right[i]

        with wave.open(self.filepath, "wb") as wf:
            wf.setnchannels(CHANNELS_STEREO)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(stereo.tobytes())

        # Restrict file permissions (owner read/write only)
        try:
            os.chmod(self.filepath, 0o600)
        except OSError:
            pass  # Windows may not support chmod

        self._caller_entries.clear()
        self._agent_entries.clear()

        return self.filepath
