"""Audio sample rate conversion utilities for bridging jambonz ↔ Gemini."""

import array


def resample(pcm_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample 16-bit mono PCM using linear interpolation.

    Args:
        pcm_bytes: Raw 16-bit signed little-endian PCM bytes.
        from_rate: Source sample rate (e.g. 24000).
        to_rate: Target sample rate (e.g. 16000).

    Returns:
        Resampled PCM bytes at the target rate.
    """
    if from_rate == to_rate:
        return pcm_bytes

    src = array.array("h")
    src.frombytes(pcm_bytes)

    if len(src) == 0:
        return b""

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
    """Interleave two mono 16-bit PCM streams into a stereo stream.

    Pads the shorter stream with silence.
    """
    left = array.array("h")
    left.frombytes(left_pcm)
    right = array.array("h")
    right.frombytes(right_pcm)

    max_len = max(len(left), len(right))

    # Pad shorter array with silence
    while len(left) < max_len:
        left.append(0)
    while len(right) < max_len:
        right.append(0)

    stereo = array.array("h", [0] * (max_len * 2))
    for i in range(max_len):
        stereo[i * 2] = left[i]
        stereo[i * 2 + 1] = right[i]

    return stereo.tobytes()
