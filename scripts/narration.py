"""Gemini text-to-speech for the demo video narration (optional; needs GEMINI_API_KEY).

Clips are cached by (model, voice, text) hash, so re-recording the video costs nothing unless
the script changes. The key is read from the environment / .env and never written anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
from pathlib import Path

import httpx

MODEL = os.environ.get("SDLC_TTS_MODEL", "gemini-3.8-flash-tts")
VOICE = os.environ.get("SDLC_TTS_VOICE", "Charon")
STYLE = "Read in a calm, clear, confident product-demo voice, at a natural pace: "
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class Narrator:
    def __init__(self, cache_dir: Path) -> None:
        self.key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self.key:
            raise RuntimeError("narration needs GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment / .env")
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)

    def clip(self, text: str) -> tuple[Path, float]:
        """Return (wav path, duration seconds) for `text`, synthesising it if not cached."""
        digest = hashlib.sha256(f"{MODEL}|{VOICE}|{text}".encode()).hexdigest()[:20]
        out = self.cache / f"{digest}.wav"
        if not out.exists():
            raw = self.cache / f"{digest}.raw.wav"
            raw.write_bytes(self._synthesise(text))
            # trim leading/trailing silence so scene timing is tight
            trim = "silenceremove=start_periods=1:start_threshold=-45dB"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(raw),
                    "-af",
                    f"{trim},areverse,{trim},areverse",
                    "-ar",
                    "24000",
                    "-ac",
                    "1",
                    str(out),
                ],
                check=True,
            )
            raw.unlink()
        return out, duration(out)

    def _synthesise(self, text: str) -> bytes:
        body = {
            "contents": [{"parts": [{"text": STYLE + text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}},
            },
        }
        for attempt in range(3):
            resp = httpx.post(
                ENDPOINT.format(model=MODEL), json=body, timeout=180, headers={"x-goog-api-key": self.key}
            )
            if resp.status_code == 200:
                part = resp.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
                data = base64.b64decode(part["data"])
                if data[:4] == b"RIFF":
                    return data
                # raw PCM (s16le, 24 kHz mono) -> wrap as WAV
                proc = subprocess.run(
                    [
                        "ffmpeg",
                        "-loglevel",
                        "error",
                        "-f",
                        "s16le",
                        "-ar",
                        "24000",
                        "-ac",
                        "1",
                        "-i",
                        "pipe:0",
                        "-f",
                        "wav",
                        "pipe:1",
                    ],
                    input=data,
                    capture_output=True,
                    check=True,
                )
                return proc.stdout
            if resp.status_code not in (429, 500, 503) or attempt == 2:
                raise RuntimeError(f"Gemini TTS failed: HTTP {resp.status_code}: {resp.text[:300]}")
        raise RuntimeError("unreachable")


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return float(out.strip())


def mix_onto(video: Path, clips: list[tuple[float, Path]], out: Path) -> None:
    """Place each clip at its offset (seconds) and mux the mixed narration onto `video`."""
    args = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video)]
    filters, labels = [], []
    for i, (offset, wav) in enumerate(clips, start=1):
        args += ["-i", str(wav)]
        ms = max(0, int(offset * 1000))
        filters.append(f"[{i}:a]adelay={ms}|{ms}[a{i}]")
        labels.append(f"[a{i}]")
    filters.append(
        f"{''.join(labels)}amix=inputs={len(clips)}:normalize=0:dropout_transition=0,"
        "loudnorm=I=-16:TP=-1.5:LRA=11,apad[aout]"
    )  # pad to full video length
    args += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "0:v",
        "-map",
        "[aout]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(out),
    ]
    subprocess.run(args, check=True)
