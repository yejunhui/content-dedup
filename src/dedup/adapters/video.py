"""Video adapter: SHA-256 plus a sequence of per-frame pHashes."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np

from ..fingerprints import PHASH_VERSION, phash_from_gray, popcount, sequence_distance
from ..models import Fingerprint
from .base import ContentAdapter, FingerprintError, MatchInfo

VIDEO_VERSION = 1


class VideoAdapter(ContentAdapter):
    name = "video"
    extensions = frozenset(
        {
            ".mp4",
            ".m4v",
            ".mov",
            ".mkv",
            ".avi",
            ".wmv",
            ".flv",
            ".webm",
            ".mpg",
            ".mpeg",
            ".m2ts",
            ".3gp",
            ".ogv",
        }
    )
    fingerprint_kinds = ("phash_seq",)

    def __init__(self, config) -> None:
        super().__init__(config)
        self.hash_size = config.video.hash_size

    def fingerprint_sig(self) -> str:
        payload = {
            "version": VIDEO_VERSION,
            "frame_interval": self.config.video.frame_interval,
            "max_frames": self.config.video.max_frames,
            "hash_size": self.hash_size,
            "color_bins": 64,
            "ffmpeg": bool(shutil.which(self.config.video.ffmpeg)),
            "ffprobe": bool(shutil.which(self.config.video.ffprobe)),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def _probe(self, path: Path) -> dict:
        executable = shutil.which(self.config.video.ffprobe)
        if not executable:
            raise FingerprintError(f"ffprobe not found: {self.config.video.ffprobe}")
        command = [
            executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=width,height",
            "-of",
            "json",
            str(path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.video.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FingerprintError("ffprobe timed out") from exc
        if result.returncode != 0:
            return {}
        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return {}

    def _extract_hashes(
        self, path: Path, interval: float, duration: float
    ) -> tuple[list[dict], np.ndarray]:
        executable = shutil.which(self.config.video.ffmpeg)
        if not executable:
            raise FingerprintError(f"ffmpeg not found: {self.config.video.ffmpeg}")
        size = self.hash_size * 4
        effective = interval
        if duration > 0 and duration / interval > self.config.video.max_frames:
            effective = duration / self.config.video.max_frames
        effective = max(effective, 0.01)
        vf = f"fps=1/{effective:.6f},scale={size}:{size}:flags=area,format=rgb24"
        command = [
            executable,
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-an",
            "-sn",
            "-dn",
            "-vf",
            vf,
            "-frames:v",
            str(self.config.video.max_frames),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=self.config.video.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FingerprintError("ffmpeg frame extraction timed out") from exc
        if result.returncode != 0 and not result.stdout:
            message = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
            raise FingerprintError(f"ffmpeg failed: {message[-1] if message else 'unknown error'}")

        frame_bytes = size * size * 3
        hashes: list[dict] = []
        color_counts = np.zeros(64, dtype=np.float64)
        for offset in range(0, len(result.stdout) - frame_bytes + 1, frame_bytes):
            rgb = np.frombuffer(
                result.stdout[offset : offset + frame_bytes], dtype=np.uint8
            ).reshape(size, size, 3)
            color_counts += _color_counts(rgb)
            gray = (
                0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
            ).round().clip(0, 255).astype(np.uint8)
            value = phash_from_gray(gray, self.hash_size)
            hashes.append({"value": value, "offset": round(len(hashes) * effective, 3)})

        if not hashes:
            hashes, color_counts = self._extract_single_frame(executable, path, size)
        if not hashes:
            raise FingerprintError("no video frames could be decoded")
        return hashes, color_counts

    def _extract_single_frame(
        self, executable: str, path: Path, size: int
    ) -> tuple[list[dict], np.ndarray]:
        command = [
            executable,
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-an",
            "-sn",
            "-dn",
            "-frames:v",
            "1",
            "-vf",
            f"scale={size}:{size}:flags=area,format=rgb24",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
        empty = np.zeros(64, dtype=np.float64)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=self.config.video.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return [], empty
        frame_bytes = size * size * 3
        if len(result.stdout) < frame_bytes:
            return [], empty
        rgb = np.frombuffer(result.stdout[:frame_bytes], dtype=np.uint8).reshape(size, size, 3)
        gray = (
            0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
        ).round().clip(0, 255).astype(np.uint8)
        return [{"value": phash_from_gray(gray, self.hash_size), "offset": 0.0}], _color_counts(rgb)

    def compute_fingerprint(self, path: Path) -> dict[str, Fingerprint]:
        probe = self._probe(path)
        duration = 0.0
        width = 0
        height = 0
        try:
            duration = float((probe.get("format") or {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        for stream in probe.get("streams") or []:
            if stream.get("width") and stream.get("height"):
                width = int(stream["width"])
                height = int(stream["height"])
                break
        interval = self.config.video.frame_interval
        if duration <= 0:
            raise FingerprintError(
                "cannot determine video duration; exact hash only "
                "(ffprobe must report format.duration)"
            )
        frames, color_counts = self._extract_hashes(path, interval, duration)
        values = [int(item["value"]) for item in frames]
        total_color = float(color_counts.sum())
        color = (
            [round(float(value / total_color), 6) for value in color_counts]
            if total_color > 0
            else []
        )
        return {
            "phash_seq": Fingerprint(
                kind="phash_seq",
                version=VIDEO_VERSION,
                value=values,
                meta={
                    "duration": round(duration, 3),
                    "width": width,
                    "height": height,
                    "frames": len(values),
                    "interval": interval,
                    "phash_version": PHASH_VERSION,
                    "frame_offsets": [item["offset"] for item in frames],
                    "color": color,
                },
            )
        }

    def match(self, kind: str, left: Fingerprint, right: Fingerprint) -> MatchInfo | None:
        if kind != "phash_seq":
            return None
        left_duration = float(left.meta.get("duration") or 0.0)
        right_duration = float(right.meta.get("duration") or 0.0)
        if left_duration > 0 and right_duration > 0:
            ratio = min(left_duration, right_duration) / max(left_duration, right_duration)
            if ratio < 1.0 - self.config.video.duration_tolerance:
                return MatchInfo(
                    similar=False,
                    detail={"duration_ratio": round(ratio, 4), "reason": "duration"},
                )
        else:
            ratio = None
        left_color = left.meta.get("color")
        right_color = right.meta.get("color")
        color_similarity = None
        if (
            isinstance(left_color, list)
            and isinstance(right_color, list)
            and len(left_color) == len(right_color)
            and left_color
        ):
            color_similarity = sum(
                min(float(a), float(b)) for a, b in zip(left_color, right_color)
            )
            if color_similarity < self.config.video.color_threshold:
                return MatchInfo(
                    similar=False,
                    detail={
                        "reason": "color histogram",
                        "color_similarity": round(color_similarity, 4),
                        "duration_ratio": round(ratio, 4) if ratio is not None else None,
                    },
                )
        frames_left = len(left.value)
        frames_right = len(right.value)
        if (
            frames_left > 0
            and frames_right > 0
            and abs(frames_left - frames_right) <= max(2, int(max(frames_left, frames_right) * 0.1))
        ):
            quick = _quick_sequence_distance(left.value, right.value)
            if quick is not None and quick > self.config.video.phash_threshold + 12:
                return MatchInfo(
                    similar=False,
                    distance=quick,
                    detail={
                        "reason": "quick-prefilter",
                        "quick_distance": round(quick, 3),
                        "duration_ratio": round(ratio, 4) if ratio is not None else None,
                    },
                )
        distance = sequence_distance(left.value, right.value)
        if distance is None:
            return MatchInfo(similar=False, detail={"reason": "no frames"})
        similar = distance <= self.config.video.phash_threshold
        return MatchInfo(
            similar=similar,
            distance=distance,
            score=1.0 - distance / 64.0,
            detail={
                "duration_ratio": round(ratio, 4) if ratio is not None else None,
                "frames_left": len(left.value),
                "frames_right": len(right.value),
                "threshold": self.config.video.phash_threshold,
                "color_similarity": (
                    round(color_similarity, 4) if color_similarity is not None else None
                ),
            },
        )


def _color_counts(rgb: np.ndarray) -> np.ndarray:
    quantized = (rgb // 64).astype(np.int64)
    indices = quantized[:, :, 0] * 16 + quantized[:, :, 1] * 4 + quantized[:, :, 2]
    return np.bincount(indices.reshape(-1), minlength=64).astype(np.float64)


def _quick_sequence_distance(left: list[int], right: list[int], samples: int = 8) -> float | None:
    """Cheap prefilter: compare evenly spaced frames before running DTW."""
    count = min(samples, len(left), len(right))
    if count < 1:
        return None
    left_indices = np.linspace(0, len(left) - 1, count, dtype=np.int64)
    right_indices = np.linspace(0, len(right) - 1, count, dtype=np.int64)
    total = 0
    for left_index, right_index in zip(left_indices, right_indices):
        total += popcount(int(left[int(left_index)]) ^ int(right[int(right_index)]))
    return total / count
