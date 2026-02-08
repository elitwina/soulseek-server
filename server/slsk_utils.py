import os
import re
from difflib import SequenceMatcher
from typing import Optional

ALLOWED_EXTS = {"flac", "mp3", "wav"}
MIN_SIZE_BYTES = 1_000_000  # ~1MB, ignore tiny/preview files


def _normalize(text: str) -> str:
	text = text.lower()
	return re.sub(r"[^a-z0-9]+", "", text)


def _basename_without_ext(path: str) -> str:
	base = os.path.basename(path)
	return base.rsplit(".", 1)[0] if "." in base else base


def _similarity(a: str, b: str) -> float:
	return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _codec_priority(ext: str) -> int:
	ext = ext.lower().lstrip(".")
	if ext == "flac":
		return 105
	if ext == "wav":
		return 101
	if ext == "mp3":
		return 80
	return 0


def _infer_mp3_bitrate_from_name(name: str) -> int:
	n = name.lower()
	if "320" in n or "cbr320" in n or "320kbps" in n:
		return 320
	if "v0" in n:
		return 245
	if "256" in n:
		return 256
	if "v1" in n:
		return 225
	if "224" in n:
		return 224
	if "192" in n:
		return 192
	if "v2" in n:
		return 190
	if "160" in n:
		return 160
	if "128" in n:
		return 128
	return 0


def _quality_tuple(filename: str, size: int, ext: str, sim: float, preferred_format: Optional[str] = None):
	ext_lower = ext.lower()
	
	# If preferred_format is specified, adjust priority
	if preferred_format:
		pref = preferred_format.lower()
		if pref == "mp3":
			# Priority: mp3 320 > flac > wav > mp3 256/192 > rest
			if ext_lower == "mp3":
				bitrate = _infer_mp3_bitrate_from_name(filename)
				if bitrate >= 320:
					return (10, bitrate, size, sim)  # Highest priority for mp3 320
				elif bitrate >= 256:
					return (3, bitrate, size, sim)  # Low priority for mp3 256 (only if mp3 preferred)
				elif bitrate >= 192:
					return (2, bitrate, size, sim)  # Very low priority for mp3 192 (only if mp3 preferred)
				else:
					return (1, bitrate, size, sim)  # Lowest priority for other mp3
			elif ext_lower == "flac":
				return (9, 0, size, sim)  # Second priority (after mp3 320)
			elif ext_lower == "wav":
				return (8, 0, size, sim)  # Third priority
			else:
				return (0, 0, size, sim)
		elif pref == "flac":
			# Priority: flac > wav > mp3 320 > mp3 other > rest
			if ext_lower == "flac":
				return (10, 0, size, sim)  # Highest priority for flac
			elif ext_lower == "wav":
				return (8, 0, size, sim)  # Second priority
			elif ext_lower == "mp3":
				bitrate = _infer_mp3_bitrate_from_name(filename)
				if bitrate >= 320:
					return (7, bitrate, size, sim)  # Third priority for mp3 320
				else:
					return (5, bitrate, size, sim)  # Lower priority for other mp3
			else:
				return (0, 0, size, sim)
	
	# Default behavior (no preference)
	codec = _codec_priority(ext)
	if ext_lower == "mp3":
		bitrate_hint = _infer_mp3_bitrate_from_name(filename)
		return (codec, bitrate_hint, size, sim)
	return (codec, 0, size, sim)


def _is_high_quality(filename: str, size: int, ext: str, sim: float) -> bool:
	ext = ext.lower()
	# Discard tiny files
	if size < 1_000_000:
		return False
	if ext in ("flac", "wav"):
		# Require reasonable album-track sizes for lossless
		# Lower similarity threshold to 0.60 to handle names with underscores, hyphens, etc.
		return size >= 20_000_000 and sim >= 0.60
	if ext == "mp3":
		br = _infer_mp3_bitrate_from_name(filename)
		# Only 320kbps passes quality gate
		# Lower similarity threshold to 0.50 to handle names with underscores, hyphens, etc.
		return (br >= 320 and size >= 4_000_000 and sim >= 0.50)
	return False



