"""Gemini text-to-speech for the demo video narration (optional; needs GEMINI_API_KEY).

Clips are cached by (model, voice, text) hash, so re-recording the video costs nothing unless
the script changes. The key is read from the environment / .env and never written anywhere.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx

MODEL = os.environ.get("SDLC_TTS_MODEL", "gemini-3.8-flash-tts")
VOICE = os.environ.get("SDLC_TTS_VOICE", "Charon")
# The voice itself sets the tone. Do NOT prefix style instructions to the text: the TTS model may
# read them aloud. Every clip is transcribed back and checked against the intended line instead.
CHECK_MODEL = os.environ.get("SDLC_TTS_CHECK_MODEL", "gemini-3.8-flash")
MIN_SIMILARITY = 0.85
TRANSCRIBE_PROMPT = (
    "Transcribe this audio verbatim. Spell out all numbers as words and letter-by-letter "
    "acronyms as separate letters. Output only the transcript."
)
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
            last = ""
            for _ in range(3):
                raw = self.cache / f"{digest}.raw.wav"
                raw.write_bytes(self._synthesise(text))
                trimmed = self.cache / f"{digest}.tmp.wav"
                _trim_silence(raw, trimmed)
                raw.unlink()
                ok, last = self.verify(trimmed, text)
                if ok:
                    trimmed.replace(out)
                    break
                trimmed.unlink()
            else:
                raise RuntimeError(f"TTS output never matched the script.\n want: {text}\n  got: {last}")
        return out, duration(out)

    def verify(self, wav: Path, text: str) -> tuple[bool, str]:
        """Transcribe the clip and require it to say the line, and only the line."""
        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "audio/wav",
                                "data": base64.b64encode(wav.read_bytes()).decode(),
                            }
                        },
                        {"text": TRANSCRIBE_PROMPT},
                    ]
                }
            ]
        }
        resp = httpx.post(
            ENDPOINT.format(model=CHECK_MODEL), json=body, timeout=180, headers={"x-goog-api-key": self.key}
        )
        resp.raise_for_status()
        heard = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return similarity(heard, text) >= MIN_SIMILARITY, heard

    def _synthesise(self, text: str) -> bytes:
        body = {
            "contents": [{"parts": [{"text": text}]}],
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


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def similarity(heard: str, wanted: str) -> float:
    """Word-sequence similarity; penalises extra spoken words (e.g. instructions read aloud)."""
    return difflib.SequenceMatcher(None, _words(heard), _words(wanted)).ratio()


def _trim_silence(src: Path, dst: Path) -> None:
    trim = "silenceremove=start_periods=1:start_threshold=-45dB"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-af",
            f"{trim},areverse,{trim},areverse",
            "-ar",
            "24000",
            "-ac",
            "1",
            str(dst),
        ],
        check=True,
    )


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return float(out.strip())


def mix_onto(video: Path, clips: list[tuple[float, Path]], out: Path) -> list[dict]:
    """Lay clips end to end at their scene offsets (exact silence in between) and mux onto `video`.

    Deterministic by construction (concat of clip + silence segments, no multi-input mixing), then
    verified: every clip's window in the final file must contain speech, or this raises.
    """
    work = out.parent / (out.stem + ".narration")
    work.mkdir(exist_ok=True)
    try:
        segments, manifest, cursor = [], [], 0.0
        for n, (offset, wav) in enumerate(sorted(clips)):
            start = max(offset, cursor)  # never overlap the previous line
            gap = start - cursor
            if gap > 0.01:
                silence = work / f"gap{n:02d}.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        "-t",
                        f"{gap:.3f}",
                        "-i",
                        "anullsrc=r=24000:cl=mono",
                        "-c:a",
                        "pcm_s16le",
                        str(silence),
                    ],
                    check=True,
                )
                segments.append(silence)
            norm = work / f"clip{n:02d}.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(wav),
                    "-ar",
                    "24000",
                    "-ac",
                    "1",
                    "-c:a",
                    "pcm_s16le",
                    str(norm),
                ],
                check=True,
            )
            segments.append(norm)
            dur = duration(norm)
            manifest.append({"clip": wav.name, "start": round(start, 3), "duration": round(dur, 3)})
            cursor = start + dur
        listing = work / "concat.txt"
        listing.write_text("".join(f"file '{seg.resolve()}'\n" for seg in segments))
        track = work / "narration.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c:a",
                "pcm_s16le",
                str(track),
            ],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video),
                "-i",
                str(track),
                "-filter_complex",
                "[1:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo,apad[a]",
                "-map",
                "0:v",
                "-map",
                "[a]",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                "48000",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out),
            ],
            check=True,
        )
        verify_coverage(out, manifest)
        (Path(".demo-audio-cache") / "last_mix.json").write_text(json.dumps(manifest, indent=1))
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def verify_coverage(media: Path, manifest: list[dict], floor_db: float = -40.0) -> None:
    """Every narration window in the final file must actually be audible."""
    silent = []
    for item in manifest:
        res = subprocess.run(
            [
                "ffmpeg",
                "-ss",
                str(item["start"]),
                "-t",
                str(max(item["duration"], 0.5)),
                "-i",
                str(media),
                "-vn",
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
        )
        m = re.search(r"mean_volume: (-?[0-9.]+) dB", res.stderr)
        level = float(m.group(1)) if m else -99.0
        item["mean_db"] = level
        if level < floor_db:
            silent.append(f"{item['clip']} @ {item['start']}s ({level} dB)")
    if silent:
        raise RuntimeError("narration missing in final audio for: " + "; ".join(silent))
