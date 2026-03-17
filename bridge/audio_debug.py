"""Audio debug logging for diagnosing call quality issues.

Collects lightweight audio statistics at each processing stage during a call.
Stats are computed periodically (~1 second intervals) using numpy vectorized ops.
"""

import json
import time
import logging
import numpy as np

logger = logging.getLogger(__name__)


class _NumpyEncoder(json.JSONEncoder):
    """Handle numpy types for JSON serialization."""
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


def compute_audio_stats(pcm_bytes: bytes, sample_rate: int) -> dict:
    """Compute audio statistics on a PCM16 buffer."""
    if not pcm_bytes or len(pcm_bytes) < 4:
        return {"rms_db": -96.0, "peak_db": -96.0, "peak_amplitude": 0,
                "zcr": 0.0, "samples": 0, "duration_ms": 0, "silence": True}

    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
    n = len(samples)
    abs_samples = np.abs(samples)

    rms = np.sqrt(np.mean(samples ** 2))
    peak = abs_samples.max()

    rms_db = 20 * np.log10(rms / 32768.0) if rms > 0 else -96.0
    peak_db = 20 * np.log10(peak / 32768.0) if peak > 0 else -96.0

    # Zero crossing rate
    signs = np.sign(samples)
    zcr = np.sum(np.abs(np.diff(signs)) > 0) / max(n - 1, 1)

    return {
        "rms_db": round(float(rms_db), 1),
        "peak_db": round(float(peak_db), 1),
        "peak_amplitude": int(peak),
        "zcr": round(float(zcr), 3),
        "samples": n,
        "duration_ms": round(n / sample_rate * 1000),
        "silence": bool(rms_db < -50.0),
    }


class AudioDebugLogger:
    """Collects audio stats at each processing stage during a call."""

    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self._start = time.monotonic()

        # Counters
        self._counters = {
            "as_frame_count": 0,
            "as_total_bytes": 0,
            "as_frame_size_min": 999999,
            "as_frame_size_max": 0,
            "gemini_send_chunks": 0,
            "gemini_send_bytes": 0,
            "gemini_recv_chunks": 0,
            "gemini_recv_bytes": 0,
            "first_audio_at": None,
            "last_audio_at": None,
            "gemini_first_recv_at": None,
        }

        # Accumulation buffers per stage
        self._pre_buf = bytearray()
        self._post_buf = bytearray()
        self._recv_buf = bytearray()
        self._play_buf = bytearray()

        # Flush thresholds (bytes) - ~1 second of audio at respective rates
        self._pre_flush = 8000 * 2       # 8kHz * 2 bytes
        self._post_flush = 16000 * 2     # 16kHz * 2 bytes
        self._recv_flush = 24000 * 2     # 24kHz * 2 bytes
        self._play_flush = 8000 * 2      # 8kHz * 2 bytes

        # Snapshots
        self._snapshots = {
            "pre_resample": [],
            "post_resample": [],
            "gemini_receive": [],
            "playback_resample": [],
        }

    def _elapsed(self) -> float:
        return round(time.monotonic() - self._start, 3)

    # --- AudioSocket reception ---
    def log_audiosocket_frame(self, payload: bytes):
        size = len(payload)
        self._counters["as_frame_count"] += 1
        self._counters["as_total_bytes"] += size
        if size < self._counters["as_frame_size_min"]:
            self._counters["as_frame_size_min"] = size
        if size > self._counters["as_frame_size_max"]:
            self._counters["as_frame_size_max"] = size
        t = self._elapsed()
        if self._counters["first_audio_at"] is None:
            self._counters["first_audio_at"] = t
        self._counters["last_audio_at"] = t

    # --- Pre-resample (raw 8kHz from Asterisk) ---
    def log_pre_resample(self, pcm_8k: bytes):
        self._pre_buf.extend(pcm_8k)
        if len(self._pre_buf) >= self._pre_flush:
            stats = compute_audio_stats(bytes(self._pre_buf), 8000)
            stats["t"] = self._elapsed()
            self._snapshots["pre_resample"].append(stats)
            self._pre_buf.clear()

    # --- Post-resample (16kHz sent to Gemini) ---
    def log_post_resample(self, pcm_16k: bytes):
        self._post_buf.extend(pcm_16k)
        if len(self._post_buf) >= self._post_flush:
            stats = compute_audio_stats(bytes(self._post_buf), 16000)
            stats["t"] = self._elapsed()
            self._snapshots["post_resample"].append(stats)
            self._post_buf.clear()

    # --- Gemini send ---
    def log_gemini_send(self, chunk_size: int):
        self._counters["gemini_send_chunks"] += 1
        self._counters["gemini_send_bytes"] += chunk_size

    # --- Gemini receive (24kHz audio) ---
    def log_gemini_receive(self, pcm_24k: bytes):
        self._counters["gemini_recv_chunks"] += 1
        self._counters["gemini_recv_bytes"] += len(pcm_24k)
        if self._counters["gemini_first_recv_at"] is None:
            self._counters["gemini_first_recv_at"] = self._elapsed()
        self._recv_buf.extend(pcm_24k)
        if len(self._recv_buf) >= self._recv_flush:
            stats = compute_audio_stats(bytes(self._recv_buf), 24000)
            stats["t"] = self._elapsed()
            self._snapshots["gemini_receive"].append(stats)
            self._recv_buf.clear()

    # --- Playback resample (24kHz -> 8kHz for Asterisk) ---
    def log_playback_resample(self, pcm_out: bytes, out_rate: int):
        self._play_buf.extend(pcm_out)
        self._play_flush = out_rate * 2
        if len(self._play_buf) >= self._play_flush:
            stats = compute_audio_stats(bytes(self._play_buf), out_rate)
            stats["t"] = self._elapsed()
            self._snapshots["playback_resample"].append(stats)
            self._play_buf.clear()

    def finalize(self) -> dict:
        """Flush remaining buffers and compute summary."""
        # Flush remaining
        if self._pre_buf:
            stats = compute_audio_stats(bytes(self._pre_buf), 8000)
            stats["t"] = self._elapsed()
            self._snapshots["pre_resample"].append(stats)
            self._pre_buf.clear()
        if self._post_buf:
            stats = compute_audio_stats(bytes(self._post_buf), 16000)
            stats["t"] = self._elapsed()
            self._snapshots["post_resample"].append(stats)
            self._post_buf.clear()
        if self._recv_buf:
            stats = compute_audio_stats(bytes(self._recv_buf), 24000)
            stats["t"] = self._elapsed()
            self._snapshots["gemini_receive"].append(stats)
            self._recv_buf.clear()
        if self._play_buf:
            stats = compute_audio_stats(bytes(self._play_buf), 8000)
            stats["t"] = self._elapsed()
            self._snapshots["playback_resample"].append(stats)
            self._play_buf.clear()

        # Fix min counter
        if self._counters["as_frame_size_min"] == 999999:
            self._counters["as_frame_size_min"] = 0

        # Compute summary
        pre_snaps = self._snapshots["pre_resample"]
        post_snaps = self._snapshots["post_resample"]
        recv_snaps = self._snapshots["gemini_receive"]

        summary = {}

        if pre_snaps:
            rms_vals = [s["rms_db"] for s in pre_snaps]
            summary["avg_caller_rms_db"] = round(sum(rms_vals) / len(rms_vals), 1)
            summary["min_caller_rms_db"] = round(min(rms_vals), 1)
            summary["max_caller_rms_db"] = round(max(rms_vals), 1)
            summary["total_caller_audio_duration_s"] = round(
                sum(s["duration_ms"] for s in pre_snaps) / 1000, 1
            )
            silence_count = sum(1 for s in pre_snaps if s["silence"])
            summary["caller_silence_ratio"] = round(silence_count / len(pre_snaps), 2)

        if recv_snaps:
            rms_vals = [s["rms_db"] for s in recv_snaps]
            summary["avg_agent_rms_db"] = round(sum(rms_vals) / len(rms_vals), 1)
            summary["total_agent_audio_duration_s"] = round(
                sum(s["duration_ms"] for s in recv_snaps) / 1000, 1
            )

        if pre_snaps and post_snaps:
            avg_pre = sum(s["rms_db"] for s in pre_snaps) / len(pre_snaps)
            avg_post = sum(s["rms_db"] for s in post_snaps) / len(post_snaps)
            summary["resample_gain_db"] = round(avg_post - avg_pre, 1)

        if self._counters["gemini_first_recv_at"] is not None:
            summary["gemini_first_response_latency_ms"] = round(
                self._counters["gemini_first_recv_at"] * 1000
            )

        # Auto-detect issues
        issues = []
        avg_rms = summary.get("avg_caller_rms_db", 0)
        if avg_rms < -40:
            issues.append("caller_audio_very_quiet (RMS: {}dB)".format(avg_rms))
        elif avg_rms < -30:
            issues.append("caller_audio_quiet (RMS: {}dB)".format(avg_rms))

        gain = summary.get("resample_gain_db", 0)
        if gain < -3:
            issues.append("resample_level_drop ({}dB)".format(gain))

        silence_ratio = summary.get("caller_silence_ratio", 0)
        if silence_ratio > 0.7:
            issues.append("high_silence_ratio ({}%)".format(int(silence_ratio * 100)))

        latency = summary.get("gemini_first_response_latency_ms", 0)
        if latency > 3000:
            issues.append("gemini_response_slow ({}ms)".format(latency))

        summary["issues"] = issues

        return {
            "counters": self._counters,
            "snapshots": self._snapshots,
            "summary": summary,
        }
