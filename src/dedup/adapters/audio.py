"""Audio adapter: SHA-256 plus optional Chromaprint (fpcalc) fingerprint."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from ..fingerprints import audio_similarity
from ..models import Fingerprint
from .base import ContentAdapter, FingerprintError, MatchInfo, UnsupportedFingerprint

AUDIO_VERSION = 1


class AudioAdapter(ContentAdapter):
    name = "audio"
    extensions = frozenset(
        {
            ".mp3",
            ".flac",
            ".wav",
            ".m4a",
            ".aac",
            ".ogg",
            ".opus",
            ".wma",
            ".aiff",
            ".aif",
            ".alac",
            ".ape",
            ".mka",
        }
    )
    fingerprint_kinds = ("chromaprint",)

    def __init__(self, config) -> None:
        super().__init__(config)
        self._fpcalc_resolved = False
        self._fpcalc: str | None = None

    def fingerprint_sig(self) -> str:
        payload = {
            "version": AUDIO_VERSION,
            "length": self.config.audio.length,
            "enabled": self.config.audio.enabled,
            "fpcalc": self.config.audio.fpcalc,
            "fpcalc_available": bool(self._resolve_fpcalc()),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def _resolve_fpcalc(self) -> str | None:
        if not self._fpcalc_resolved:
            self._fpcalc = shutil.which(self.config.audio.fpcalc)
            self._fpcalc_resolved = True
        return self._fpcalc

    def compute_fingerprint(self, path: Path) -> dict[str, Fingerprint]:
        if not self.config.audio.enabled:
            raise UnsupportedFingerprint("audio fingerprinting is disabled")
        executable = self._resolve_fpcalc()
        if not executable:
            raise UnsupportedFingerprint(
                "fpcalc not found; install chromaprint (Debian: libchromaprint-tools) "
                "for audio fingerprinting"
            )
        command = [
            executable,
            "-json",
            "-raw",
            "-length",
            str(self.config.audio.length),
            str(path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.audio.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FingerprintError("fpcalc timed out") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip().splitlines()
            raise FingerprintError(f"fpcalc failed: {message[-1] if message else 'unknown error'}")
        try:
            payload = json.loads(result.stdout)
            raw = payload.get("fingerprint") or []
            if isinstance(raw, str):
                raw = [part for part in raw.split(",") if part]
            values = [int(item) for item in raw]
            duration = float(payload.get("duration") or 0.0)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise FingerprintError(f"cannot parse fpcalc output: {exc}") from exc
        if not values:
            raise FingerprintError("fpcalc returned an empty fingerprint")
        return {
            "chromaprint": Fingerprint(
                kind="chromaprint",
                version=AUDIO_VERSION,
                value=values,
                meta={
                    "duration": round(duration, 3),
                    "frames": len(values),
                    "length": self.config.audio.length,
                },
            )
        }

    def match(self, kind: str, left: Fingerprint, right: Fingerprint) -> MatchInfo | None:
        if kind != "chromaprint":
            return None
        left_duration = float(left.meta.get("duration") or 0.0)
        right_duration = float(right.meta.get("duration") or 0.0)
        ratio = None
        if left_duration > 0 and right_duration > 0:
            ratio = min(left_duration, right_duration) / max(left_duration, right_duration)
            if ratio < 1.0 - self.config.audio.duration_tolerance:
                return MatchInfo(
                    similar=False,
                    detail={"duration_ratio": round(ratio, 4), "reason": "duration"},
                )
        score, shift, overlap = audio_similarity(
            left.value,
            right.value,
            max_shift=self.config.audio.max_shift,
            min_overlap=self.config.audio.min_overlap,
        )
        similar = score >= self.config.audio.min_score
        return MatchInfo(
            similar=similar,
            score=score,
            detail={
                "shift": shift,
                "overlap": overlap,
                "duration_ratio": round(ratio, 4) if ratio is not None else None,
                "threshold": self.config.audio.min_score,
            },
        )
