"""Optional local multilingual voice transcription; no hosted service credentials.

whisper.cpp and a multilingual model are operator-installed prerequisites, not
bundled dependencies. Tests use a stub process runner; no ASR quality claim is made.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request

MAX_AUDIO_BYTES = 16 * 1024 * 1024


class VoiceError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise VoiceError("Audio redirects are not permitted")


def _request(request: urllib.request.Request, limit: int, transport=None) -> bytes:
    try:
        if transport is not None:
            status, data = transport(request, timeout=10)
            if status != 200 or len(data) > limit:
                raise VoiceError("Audio provider request failed")
            return data
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=10) as reply:
            data = reply.read(limit + 1)
            if reply.status != 200 or len(data) > limit:
                raise VoiceError("Audio provider request failed")
            return data
    except Exception:
        # Request paths can contain Telegram tokens and URLs can carry CDN signatures.
        raise VoiceError("Audio provider request failed") from None


def telegram_file(token: str, file_id: str, *, transport=None,
                  max_bytes: int = MAX_AUDIO_BYTES) -> tuple[bytes, str]:
    """Resolve/download a Telegram file on its official fixed origin only."""
    if not token or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise VoiceError("Telegram file credentials are not configured")
    if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", file_id):
        raise VoiceError("Invalid Telegram file reference")
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/getFile",
        data=json.dumps({"file_id": file_id}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        payload = json.loads(_request(request, 1024 * 1024, transport))
        result = payload["result"]
        if payload.get("ok") is not True or not isinstance(result, dict):
            raise ValueError()
        path = result["file_path"]
        if (not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,512}", path)
                or any(part in {"", ".", ".."} for part in path.split("/"))):
            raise ValueError()
        size = result.get("file_size", 0)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > max_bytes:
            raise ValueError()
        url = "https://api.telegram.org/file/bot" + token + "/" + path
        content = _request(urllib.request.Request(url), max_bytes, transport)
        return content, path
    except Exception:
        raise VoiceError("Telegram file download failed") from None


def instagram_audio(url: str, *, transport=None) -> bytes:
    """Fetch only Meta-owned media origins from an authenticated webhook."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        allowed = (host.endswith(".cdninstagram.com") or host.endswith(".fbcdn.net")
                   or host == "lookaside.fbsbx.com")
        if (parsed.scheme != "https" or not allowed or parsed.username or parsed.password
                or parsed.port not in {None, 443}):
            raise ValueError()
        return _request(urllib.request.Request(url), MAX_AUDIO_BYTES, transport)
    except Exception:
        raise VoiceError("Instagram audio download failed") from None


class WhisperCppTranscriber:
    def __init__(self, executable: str, model: str, ffmpeg: str = "", *, runner=subprocess.run,
                 timeout: float = 25) -> None:
        if not executable or not model or timeout <= 0 or timeout > 60:
            raise VoiceError("Configure a local whisper.cpp executable and multilingual model")
        self.executable, self.model, self.ffmpeg = executable, model, ffmpeg
        self.runner, self.timeout = runner, timeout

    @classmethod
    def from_environment(cls):
        executable = os.environ.get("WHISPER_CPP_BIN", "")
        model = os.environ.get("WHISPER_MODEL_PATH", "")
        if not executable and not model:
            return None
        return cls(executable, model, os.environ.get("FFMPEG_BIN", ""))

    def _run(self, arguments: list[str]) -> None:
        try:
            result = self.runner(arguments, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=self.timeout, check=False,
                                 env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"})
            if result.returncode != 0:
                raise ValueError()
        except Exception:
            raise VoiceError("Local transcription failed") from None

    def transcribe(self, audio: bytes, language: str = "auto") -> str:
        if language not in {"auto", "uz", "ru"}:
            raise VoiceError("Only automatic, Uzbek or Russian transcription is configured")
        if not isinstance(audio, bytes) or not audio or len(audio) > MAX_AUDIO_BYTES:
            raise VoiceError("Audio is empty or exceeds the local limit")
        with tempfile.TemporaryDirectory(prefix="estate-voice-") as directory:
            root = Path(directory)
            source = root / "input.audio"
            source.write_bytes(audio)
            wave = root / "input.wav"
            if self.ffmpeg:
                self._run([self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                           "-i", str(source), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wave)])
            elif audio.startswith(b"RIFF") and audio[8:12] == b"WAVE":
                wave.write_bytes(audio)
            else:
                raise VoiceError("Configure ffmpeg for platform audio formats")
            output = root / "transcript"
            self._run([self.executable, "-m", self.model, "-f", str(wave),
                       "-l", language, "-otxt", "-of", str(output)])
            try:
                file = output.with_suffix(".txt")
                if file.stat().st_size > 32 * 1024:
                    raise ValueError()
                text = file.read_text(encoding="utf-8").strip()
                if not text:
                    raise ValueError()
                return text
            except Exception:
                raise VoiceError("Transcription produced no usable text") from None
