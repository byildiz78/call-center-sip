"""Audio sample rate conversion utilities.

Uses soxr (libsoxr) for high-quality resampling with proper
anti-aliasing filters. Falls back to linear interpolation if
soxr is not available.
"""

import array
import numpy as np

try:
    import soxr
    HAS_SOXR = True
except ImportError:
    HAS_SOXR = False


def resample(pcm_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample 16-bit mono PCM audio.

    Uses soxr for high-quality resampling with anti-aliasing.
    """
    if from_rate == to_rate or not pcm_bytes:
        return pcm_bytes

    if HAS_SOXR:
        # Convert bytes to float32 numpy array
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        # Resample with soxr (HQ quality)
        resampled = soxr.resample(samples, from_rate, to_rate, quality="HQ")
        # Convert back to int16 bytes
        out = np.clip(resampled * 32768.0, -32768, 32767).astype(np.int16)
        return out.tobytes()

    # Fallback: linear interpolation
    src = array.array("h")
    src.frombytes(pcm_bytes)
    ratio = from_rate / to_rate
    out_len = int(len(src) / ratio)
    dst = array.array("h", [0] * out_len)
    for i in range(out_len):
        pos = i * ratio
        idx = int(pos)
        frac = pos - idx
        if idx + 1 < len(src):
            dst[i] = int(src[idx] * (1.0 - frac) + src[idx + 1] * frac)
        else:
            dst[i] = src[min(idx, len(src) - 1)]
    return dst.tobytes()


def resample_24k_to_16k(pcm_bytes: bytes) -> bytes:
    return resample(pcm_bytes, 24000, 16000)


def resample_16k_to_24k(pcm_bytes: bytes) -> bytes:
    return resample(pcm_bytes, 16000, 24000)


def resample_8k_to_16k(pcm_bytes: bytes) -> bytes:
    return resample(pcm_bytes, 8000, 16000)


def resample_16k_to_8k(pcm_bytes: bytes) -> bytes:
    return resample(pcm_bytes, 16000, 8000)


def mix_stereo(left_pcm: bytes, right_pcm: bytes) -> bytes:
    """Interleave two mono 16-bit PCM streams into a stereo stream."""
    left = np.frombuffer(left_pcm, dtype=np.int16)
    right = np.frombuffer(right_pcm, dtype=np.int16)

    max_len = max(len(left), len(right))
    if len(left) < max_len:
        left = np.pad(left, (0, max_len - len(left)))
    if len(right) < max_len:
        right = np.pad(right, (0, max_len - len(right)))

    stereo = np.empty(max_len * 2, dtype=np.int16)
    stereo[0::2] = left
    stereo[1::2] = right
    return stereo.tobytes()
