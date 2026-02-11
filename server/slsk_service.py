import asyncio
import os
import re
import time
import logging
import aiohttp
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import AsyncIterator, Iterable, Optional

# For reading audio metadata
try:
	import mutagen
	from mutagen.easyid3 import EasyID3
	from mutagen.flac import FLAC
	MUTAGEN_AVAILABLE = True
except ImportError:
	MUTAGEN_AVAILABLE = False

# Suppress noisy UPnP and aioslsk debug/warning messages
logging.getLogger('aioslsk').setLevel(logging.CRITICAL)
logging.getLogger('aioslsk.network').setLevel(logging.CRITICAL)
logging.getLogger('aioslsk.upnp').setLevel(logging.CRITICAL)
logging.getLogger('asyncio').setLevel(logging.CRITICAL)

# Custom exception handler to suppress non-critical connection errors
def _custom_exception_handler(loop, context):
	exception = context.get('exception')
	if exception:
		exc_name = type(exception).__name__
		# Suppress expected connection errors
		if 'ConnectionFailedError' in exc_name or 'ConnectionRefusedError' in exc_name:
			return  # Silently ignore
		if 'failed to connect' in str(exception).lower():
			return  # Silently ignore
	# For other exceptions, use default handler
	loop.default_exception_handler(context)

from aioslsk.client import SoulSeekClient
from aioslsk.settings import Settings, CredentialsSettings
from aioslsk.network.network import ListeningConnectionErrorMode
from aioslsk.events import (
	SearchResultEvent,
	SearchRequestRemovedEvent,
	TransferAddedEvent,
	TransferProgressEvent,
	TransferRemovedEvent,
)
from aioslsk.transfer.manager import TransferState
from aioslsk.protocol.primitives import AttributeKey

ALLOWED_EXTS = {"flac", "mp3", "wav"}
MIN_SIZE_BYTES = 1_000_000  # ~1MB, ignore tiny/preview files


def _get_bitrate_from_attributes(attributes: list) -> int:
	"""Extract bitrate from file attributes if available."""
	for attr in attributes:
		if attr.key == AttributeKey.BITRATE.value:
			return attr.value
	return 0


def _rename_from_metadata(file_path: str) -> Optional[str]:
	"""Rename file based on metadata (artist - title) if available.

	Returns the new path if renamed, None if not renamed.
	"""
	if not MUTAGEN_AVAILABLE:
		return None

	if not os.path.exists(file_path):
		return None

	try:
		ext = os.path.splitext(file_path)[1].lower()
		artist = None
		title = None

		if ext == '.mp3':
			try:
				audio = EasyID3(file_path)
				artist = audio.get('artist', [''])[0]
				title = audio.get('title', [''])[0]
			except Exception:
				# Fallback to generic mutagen
				audio = mutagen.File(file_path, easy=True)
				if audio:
					artist = audio.get('artist', [''])[0] if audio.get('artist') else None
					title = audio.get('title', [''])[0] if audio.get('title') else None
		elif ext == '.flac':
			audio = FLAC(file_path)
			artist = audio.get('artist', [''])[0] if audio.get('artist') else None
			title = audio.get('title', [''])[0] if audio.get('title') else None
		else:
			# Try generic approach for other formats
			audio = mutagen.File(file_path, easy=True)
			if audio:
				artist = audio.get('artist', [''])[0] if audio.get('artist') else None
				title = audio.get('title', [''])[0] if audio.get('title') else None

		# Only rename if we have both artist and title
		if artist and title:
			# Clean up artist and title for filename (remove invalid characters)
			def clean_filename(s: str) -> str:
				# Remove characters not allowed in filenames
				s = re.sub(r'[<>:"/\\|?*]', '', s)
				# Remove leading/trailing whitespace and dots
				s = s.strip().strip('.')
				return s

			clean_artist = clean_filename(artist)
			clean_title = clean_filename(title)

			if clean_artist and clean_title:
				new_name = f"{clean_artist} - {clean_title}{ext}"
				new_path = os.path.join(os.path.dirname(file_path), new_name)

				# Check if the current name is already correct
				current_name = os.path.basename(file_path)
				if current_name == new_name:
					return None  # Already correct

				# Check if target file already exists
				if os.path.exists(new_path):
					# Add a number suffix to avoid overwriting
					base_new_name = f"{clean_artist} - {clean_title}"
					counter = 1
					while os.path.exists(new_path):
						new_name = f"{base_new_name} ({counter}){ext}"
						new_path = os.path.join(os.path.dirname(file_path), new_name)
						counter += 1

				os.rename(file_path, new_path)
				print(f"\033[96m[RENAME] Renamed to: {new_name}\033[0m")
				return new_path

		return None
	except Exception as e:
		print(f"\033[93m[RENAME] Could not read metadata: {e}\033[0m")
		return None


def _normalize(text: str) -> str:
	text = text.lower()
	return re.sub(r"[^a-z0-9]+", "", text)


def _basename_without_ext(path: str) -> str:
	base = os.path.basename(path)
	return base.rsplit(".", 1)[0] if "." in base else base


def _basename_from_path(full_path: str) -> str:
	"""Return only the filename (last path component) for display/comparison. Soulseek paths use backslash."""
	parts = full_path.replace("\\", "/").split("/")
	return parts[-1].strip() if parts else full_path


def _normalize_search_query(query: str) -> str:
	"""Normalize query for better Soulseek search results.

	Removes special characters like commas, dashes, parentheses that may
	reduce search matches, while keeping all words intact.
	"""
	# Replace special characters with spaces
	normalized = re.sub(r'[,\-–—\(\)\[\]\.]+', ' ', query)
	# Collapse multiple spaces into one
	normalized = re.sub(r'\s+', ' ', normalized)
	# Strip leading/trailing whitespace
	return normalized.strip()


def _remove_track_number_prefix(name: str, query: str) -> str:
	"""Remove track number prefixes like '109 - ', '03 - ', '1-03 ' from filename.

	Only removes if the number is NOT part of the original query.
	E.g., for query "50 cent - candy shop", we keep "50" in the name.
	"""
	# Pattern: starts with optional disc number, track number, then separator
	# Examples: "109 - ", "03 - ", "1-03 ", "03.", "03 "
	import re

	# Check if query starts with a number (like "50 cent")
	query_starts_with_number = re.match(r'^\d+', query.strip())
	query_number = query_starts_with_number.group() if query_starts_with_number else None

	# Pattern for track number prefix: number(s) followed by separator (-, ., space)
	# Can also be "disc-track" format like "1-03"
	pattern = r'^(\d{1,2}[-.]?\d{0,2})\s*[-.\s]+\s*'
	match = re.match(pattern, name)

	if match:
		prefix_number = re.match(r'^\d+', match.group(1)).group() if re.match(r'^\d+', match.group(1)) else None
		# Only remove if the prefix number is NOT the same as query's starting number
		if query_number and prefix_number == query_number:
			return name  # Keep it, it's part of the search
		return name[match.end():]  # Remove the prefix

	return name


# Keywords that indicate a different version of a song
# If these appear in filename but NOT in query, filter out the result
NEGATIVE_VERSION_KEYWORDS = {
	# Remix variations
	'remix', 'remixed', 'remixes', 'rmx',
	# Extended variations
	'extended', 'extended mix', 'extended version', 'extended edit', 'ext mix', 'ext version',
	# Live variations
	'live', 'live version', 'live recording', 'concert',
	# Instrumental/Acapella
	'instrumental', 'inst', 'acapella', 'acappella', 'a cappella',
	# Cover/Karaoke
	'karaoke', 'cover', 'tribute',
	# DJ edits
	'mashup', 'mash up', 'mash-up', 'blend', 'bootleg', 'vip', 'flip', 'dub', 'dub mix',
	# Acoustic
	'acoustic', 'acoustic version', 'unplugged',
	# Other versions
	'demo', 'session', 'alternate', 'alt version', 'original mix',
	# Hebrew
	'רמיקס', 'לייב', 'הופעה חיה', 'אקוסטי', 'קאבר', 'גרסה מורחבת', 'אקסטנדד',
}

# Keywords that are OK even if not in query (they're the "standard" version)
ACCEPTABLE_VERSION_KEYWORDS = {
	'radio', 'radio edit', 'radio version', 'single', 'album version',
	'clean', 'dirty', 'explicit', 'remaster', 'remastered',
}


def _has_unwanted_version(filename: str, query: str) -> bool:
	"""Check if filename contains version keywords that weren't in the query.

	Returns True if the file should be filtered out.
	"""
	filename_lower = filename.lower()
	query_lower = query.lower()

	# Check each negative keyword
	for keyword in NEGATIVE_VERSION_KEYWORDS:
		if keyword in filename_lower and keyword not in query_lower:
			return True  # Filter out

	return False  # OK to keep


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


def _get_effective_bitrate(filename: str, size: int, stored_bitrate: int) -> int:
	"""Get effective bitrate from stored value, filename, or size estimation."""
	if stored_bitrate > 0:
		return stored_bitrate
	from_name = _infer_mp3_bitrate_from_name(filename)
	if from_name > 0:
		return from_name
	# Estimate from size (assuming ~4 min song)
	if size >= 9_000_000:
		return 320
	elif size >= 5_500_000:
		return 192
	return 128


def _quality_tuple(filename: str, size: int, ext: str, sim: float, preferred_format: Optional[str] = None):
	ext_lower = ext.lower()

	# If preferred_format is specified, adjust priority
	if preferred_format:
		pref = preferred_format.lower()
		if pref == "mp3":
			# Priority: 1. mp3 320, 2. flac, 3. wav, 4. mp3 256, 5. mp3 192
			if ext_lower == "mp3":
				bitrate = _infer_mp3_bitrate_from_name(filename)
				if bitrate >= 320:
					return (10, bitrate, size, sim)  # 1st priority: mp3 320
				elif bitrate >= 256:
					return (7, bitrate, size, sim)  # 4th priority: mp3 256
				elif bitrate >= 192:
					return (6, bitrate, size, sim)  # 5th priority: mp3 192
				else:
					return (1, bitrate, size, sim)  # Lowest priority for other mp3
			elif ext_lower == "flac":
				return (9, 0, size, sim)  # 2nd priority: flac
			elif ext_lower == "wav":
				return (8, 0, size, sim)  # 3rd priority: wav
			else:
				return (0, 0, size, sim)
		elif pref == "flac":
			# Priority: 1. flac, 2. mp3 320, 3. wav, 4. mp3 256, 5. mp3 192
			if ext_lower == "flac":
				return (10, 0, size, sim)  # 1st priority: flac
			elif ext_lower == "mp3":
				bitrate = _infer_mp3_bitrate_from_name(filename)
				if bitrate >= 320:
					return (9, bitrate, size, sim)  # 2nd priority: mp3 320
				elif bitrate >= 256:
					return (7, bitrate, size, sim)  # 4th priority: mp3 256
				elif bitrate >= 192:
					return (6, bitrate, size, sim)  # 5th priority: mp3 192
				else:
					return (4, bitrate, size, sim)  # Lower priority for other mp3
			elif ext_lower == "wav":
				return (8, 0, size, sim)  # 3rd priority: wav
			else:
				return (0, 0, size, sim)

	# Default behavior (no preference)
	codec = _codec_priority(ext)
	if ext_lower == "mp3":
		bitrate_hint = _infer_mp3_bitrate_from_name(filename)
		return (codec, bitrate_hint, size, sim)
	return (codec, 0, size, sim)


def _is_perfect_match(filename: str, size: int, ext: str, sim: float, preferred_format: Optional[str] = None, bitrate: int = 0, query: str = "") -> bool:
	"""Check if a file is a PERFECT match that we should start downloading immediately.

	Rules:
	- Similarity must be >= 0.75 (high threshold to avoid wrong songs)
	- If query has artist (contains " - "), artist name must appear in filename
	- Must not contain unwanted version keywords (remix, live, etc.) unless query has them
	- If user wants MP3: ONLY MP3 320 is perfect
	- If user wants FLAC: ONLY FLAC is perfect
	- No preference: FLAC or MP3 320 are perfect

	Radio edit/clean/dirty versions are acceptable as "regular" versions.
	"""
	ext = ext.lower()

	# Similarity threshold for perfect match - high threshold to avoid downloading wrong songs
	if sim < 0.75:
		return False

	# If query has artist (Artist - Title format), verify artist appears in filename
	if query and " - " in query:
		artist = query.split(" - ")[0].strip().lower()
		filename_lower = filename.lower()
		# Check if at least the first word of the artist name appears in the filename
		artist_words = artist.split()
		if artist_words:
			first_artist_word = artist_words[0]
			if len(first_artist_word) >= 3 and first_artist_word not in filename_lower:
				return False  # Artist doesn't appear in filename - not a perfect match

	# Filter out unwanted versions (remix, live, etc.)
	if query and _has_unwanted_version(filename, query):
		return False

	def _is_320_mp3(fname: str, fsize: int, br: int) -> bool:
		# First priority: actual bitrate from attributes (must be 320+)
		if br >= 320:
			return True
		# Second: check filename for bitrate hints
		br_from_name = _infer_mp3_bitrate_from_name(fname)
		if br_from_name >= 320:
			return True
		# Third: if no bitrate info at all, estimate from size (9MB+ suggests 320kbps)
		if br == 0 and br_from_name == 0 and fsize >= 9_000_000:
			return True
		return False

	if preferred_format:
		pref = preferred_format.lower()
		if pref == "mp3":
			# ONLY MP3 320 is perfect - nothing else triggers early download
			if ext == "mp3":
				return _is_320_mp3(filename, size, bitrate)
			return False
		elif pref == "flac":
			# ONLY FLAC is perfect - nothing else triggers early download
			if ext == "flac":
				return size >= 20_000_000
			return False
	else:
		# No preference - FLAC or MP3 320 are perfect
		if ext == "flac":
			return size >= 20_000_000
		if ext == "mp3":
			return _is_320_mp3(filename, size, bitrate)
	return False


def _is_high_quality(filename: str, size: int, ext: str, sim: float) -> bool:
	ext = ext.lower()
	# Discard tiny files
	if size < 1_000_000:
		return False
	if ext in ("flac", "wav"):
		# Require reasonable album-track sizes for lossless
		return size >= 20_000_000 and sim >= 0.75
	if ext == "mp3":
		br = _infer_mp3_bitrate_from_name(filename)
		# Only 320kbps passes quality gate
		return (br >= 320 and size >= 4_000_000 and sim >= 0.65)
	return False


@dataclass
class DownloadEvent:
	kind: str  # 'status' | 'progress' | 'finished' | 'error' | 'started' | 'files_list'
	message: str = ""
	percent: Optional[int] = None
	path: Optional[str] = None
	files_list: Optional[list[str]] = None  # List of candidate file names for client to check


class SoulseekService:
	def __init__(self, username: str, password: str, download_dir: str, search_timeout: int = 10, job_id: Optional[str] = None):
		self.username = username
		self.password = password
		self.download_dir = download_dir
		self.search_timeout = search_timeout
		# Create unique subdirectory for this job to avoid file conflicts
		self.job_id = job_id or os.urandom(6).hex()
		self.job_download_dir = os.path.join(download_dir, self.job_id)

	async def _download_one(self, query: str, preferred_format: Optional[str] = None, confirmation_event: Optional[asyncio.Event] = None) -> AsyncIterator[DownloadEvent]:
		# Set custom exception handler to suppress connection errors
		loop = asyncio.get_event_loop()
		loop.set_exception_handler(_custom_exception_handler)

		os.makedirs(self.job_download_dir, exist_ok=True)
		settings = Settings(credentials=CredentialsSettings(username=self.username, password=self.password))
		settings.shares.download = self.job_download_dir
		settings.searches.send.request_timeout = self.search_timeout
		# Enable UPnP to help with port forwarding through NAT/firewall
		settings.network.upnp.enabled = True
		# Try using random high ports that might work better
		# Use ports in the ephemeral range (49152-65535) that are less likely to be blocked
		import random
		settings.network.listening.port = random.randint(49152, 65535)
		settings.network.listening.obfuscated_port = random.randint(49152, 65535)
		# Use ALL mode - only fail if ALL connections fail
		settings.network.listening.error_mode = ListeningConnectionErrorMode.ALL

		# Store: (username, filename, size, ext, bitrate)
		collected: list[tuple[str, str, int, str, int]] = []
		stop_event = asyncio.Event()
		event_queue: asyncio.Queue[DownloadEvent] = asyncio.Queue(maxsize=1000)
		target = _basename_without_ext(query)

		# Queue for perfect matches to try downloading immediately
		perfect_queue: asyncio.Queue[tuple[str, str, int, str, int, float]] = asyncio.Queue()
		download_success = asyncio.Event()  # Set when download succeeds
		failed_users: set[str] = set()  # Track failed users across attempts

		# Try to connect with retry logic for listening port issues
		max_retries = 3
		for attempt in range(max_retries):
			try:
				async with SoulSeekClient(settings=settings) as client:
					await client.login()

					async def on_result(event: SearchResultEvent):
						for file in list(event.result.shared_items) + list(event.result.locked_results):
							# Don't process more results if download already succeeded
							if download_success.is_set():
								return

							ext = (file.extension or os.path.splitext(file.filename)[1][1:]).lower()
							if ext not in ALLOWED_EXTS:
								continue
							fsize = int(file.filesize)
							if fsize < MIN_SIZE_BYTES:
								continue
							# Get bitrate from attributes if available
							bitrate = _get_bitrate_from_attributes(file.attributes) if hasattr(file, 'attributes') else 0

							# Skip MP3s below 192kbps
							if ext == "mp3":
								br_from_name = _infer_mp3_bitrate_from_name(file.filename)
								# Determine effective bitrate
								effective_bitrate = bitrate if bitrate > 0 else br_from_name
								# If no bitrate info, estimate from size
								if effective_bitrate == 0:
									# 192kbps ~ 5.7MB for 4min, 320kbps ~ 9.6MB for 4min
									if fsize >= 9_000_000:
										effective_bitrate = 320
									elif fsize >= 5_500_000:
										effective_bitrate = 192
									else:
										effective_bitrate = 128  # Assume low quality
								# Skip anything below 192kbps
								if effective_bitrate < 192:
									continue

							result_tuple = (event.result.username, file.filename, fsize, ext, bitrate)
							collected.append(result_tuple)

							# Calculate similarity and print in real-time
							basename = _basename_without_ext(file.filename)
							clean_name = _remove_track_number_prefix(basename, query)
							sim = _similarity(clean_name, target)
							size_mb = fsize / (1024 * 1024)
							bitrate_str = f"{bitrate}kbps" if bitrate > 0 else "?"
							is_perfect = _is_perfect_match(file.filename, fsize, ext, sim, preferred_format, bitrate, query)
							perfect_marker = " ⚡PERFECT" if is_perfect else ""
							print(f"\033[93m[{len(collected)}] sim={sim:.2f} | {ext} | {bitrate_str} | {size_mb:.1f}MB | {event.result.username} | {clean_name}{perfect_marker}\033[0m")

							# If perfect match, add to queue for immediate download attempt
							if is_perfect and event.result.username not in failed_users:
								print(f"\033[95m[SEARCH] ⚡ Perfect match found! Queuing for immediate download...\033[0m")
								perfect_queue.put_nowait((event.result.username, file.filename, fsize, ext, bitrate, sim))

					async def on_removed(event: SearchRequestRemovedEvent):
						stop_event.set()

					client.events.register(SearchResultEvent, on_result)
					client.events.register(SearchRequestRemovedEvent, on_removed)

					# Normalize query for better search results (remove special chars)
					search_query = _normalize_search_query(query)
					search_request = await client.searches.search(search_query)
					if search_query != query:
						yield DownloadEvent(kind="status", message=f"searching '{search_query}' (normalized from '{query}') ({self.search_timeout}s)")
					else:
						yield DownloadEvent(kind="status", message=f"searching '{query}' ({self.search_timeout}s)")

					# Helper function to try downloading a file
					async def try_download(username: str, filename: str, size: int, ext: str) -> AsyncIterator[DownloadEvent]:
						"""Try to download a file. Yields events. Returns True if successful."""
						nonlocal failed_users

						progress_started = asyncio.Event()
						complete_or_removed = asyncio.Event()
						last_percent: Optional[int] = None
						finished = False
						started_sent = False
						finished_success = False
						queued_notified = False
						download_queue_inner: asyncio.Queue[DownloadEvent] = asyncio.Queue(maxsize=1000)

						async def _finish_if_needed(transfer, curr_bytes: int):
							nonlocal last_percent, finished, finished_success
							fs = transfer.filesize or 0
							local_ok = False
							try:
								if transfer.local_path and os.path.exists(transfer.local_path):
									local_size = os.path.getsize(transfer.local_path)
									local_ok = fs > 0 and local_size >= fs
							except Exception:
								pass
							if fs > 0 and curr_bytes >= fs and (last_percent or 0) < 100:
								last_percent = 100
								await download_queue_inner.put(DownloadEvent(kind="progress", percent=100, message="100%"))
								finished_success = True
							if (last_percent or 0) >= 100 or local_ok:
								finished_success = True
							if not finished:
								finished = True
								if finished_success:
									await download_queue_inner.put(DownloadEvent(kind="finished", path=transfer.local_path or ""))
								else:
									await download_queue_inner.put(DownloadEvent(kind="status", message="failed, trying next"))
								complete_or_removed.set()

						async def on_removed_t(event: TransferRemovedEvent):
							if event.transfer.username == username and event.transfer.remote_path == filename:
								await _finish_if_needed(event.transfer, event.transfer.bytes_transfered)

						async def on_progress_t(event: TransferProgressEvent):
							nonlocal started_sent, last_percent, queued_notified
							for transfer, prev, curr in event.updates:
								if transfer.username != username or transfer.remote_path != filename:
									continue
								fs = transfer.filesize or 0
								if curr.state == TransferState.State.QUEUED and not queued_notified:
									queued_notified = True
									await download_queue_inner.put(DownloadEvent(kind="status", message="queued at source"))
								if (curr.bytes_transfered > 0 or curr.state == TransferState.State.DOWNLOADING) and not progress_started.is_set():
									progress_started.set()
									if not started_sent:
										started_sent = True
										await download_queue_inner.put(DownloadEvent(kind="started", path=transfer.local_path or ""))
								if fs > 0 and curr.bytes_transfered >= 0:
									percent = int((curr.bytes_transfered / fs) * 100)
									percent = max(1, min(100, percent))
									prev_p = 0 if last_percent is None else last_percent
									if percent > prev_p:
										for p in range(prev_p + 1, percent + 1):
											await download_queue_inner.put(DownloadEvent(kind="progress", percent=p, message=f"{p}%"))
										last_percent = percent
								if curr.state in (
									TransferState.State.COMPLETE,
									TransferState.State.INCOMPLETE,
									TransferState.State.ABORTED,
									TransferState.State.FAILED,
								):
									finished_success_local = (curr.state == TransferState.State.COMPLETE) or (fs > 0 and curr.bytes_transfered >= fs)
									if finished_success_local:
										finished_success = True
									progress_started.set()
									await _finish_if_needed(transfer, curr.bytes_transfered)

						client.events.register(TransferRemovedEvent, on_removed_t)
						client.events.register(TransferProgressEvent, on_progress_t)

						try:
							transfer = await client.transfers.download(username, filename)

							queued_and_stuck = asyncio.Event()

							async def monitor_queue():
								await asyncio.sleep(3.0)
								if queued_notified and not progress_started.is_set():
									queued_and_stuck.set()

							monitor_task = asyncio.create_task(monitor_queue())

							try:
								done, pending = await asyncio.wait(
									[asyncio.create_task(progress_started.wait()), monitor_task],
									timeout=10.0,
									return_when=asyncio.FIRST_COMPLETED
								)

								for task in pending:
									task.cancel()
									try:
										await task
									except asyncio.CancelledError:
										pass

								if queued_and_stuck.is_set():
									await client.transfers.abort(transfer)
									failed_users.add(username)
									yield DownloadEvent(kind="status", message=f"{username} stuck in queue, skipping")
									return

								if not progress_started.is_set():
									await client.transfers.abort(transfer)
									failed_users.add(username)
									yield DownloadEvent(kind="status", message=f"start timeout from {username}, trying next")
									return
							except asyncio.TimeoutError:
								await client.transfers.abort(transfer)
								failed_users.add(username)
								yield DownloadEvent(kind="status", message=f"start timeout from {username}, trying next")
								return

							# Download in progress
							last_progress_time = time.time()
							stall_timeout = 10.0

							while not complete_or_removed.is_set():
								try:
									ev = await asyncio.wait_for(download_queue_inner.get(), timeout=0.5)
									if ev.kind == "progress":
										last_progress_time = time.time()
										yield ev
									elif ev.kind == "finished":
										# Try to rename file based on metadata
										if ev.path:
											new_path = _rename_from_metadata(ev.path)
											if new_path:
												ev = DownloadEvent(kind="finished", path=new_path)
										yield ev
										download_success.set()
										return
									else:
										yield ev
								except asyncio.TimeoutError:
									if time.time() - last_progress_time > stall_timeout and last_percent is not None and last_percent < 100:
										await client.transfers.abort(transfer)
										failed_users.add(username)
										yield DownloadEvent(kind="status", message=f"Download stalled at {last_percent}% from {username}, trying next")
										complete_or_removed.set()
										return

							# Drain remaining events
							while not download_queue_inner.empty():
								ev = await download_queue_inner.get()
								if ev.kind == "finished":
									# Try to rename file based on metadata
									if ev.path:
										new_path = _rename_from_metadata(ev.path)
										if new_path:
											ev = DownloadEvent(kind="finished", path=new_path)
									yield ev
									download_success.set()
									return
								yield ev

							if not finished_success:
								failed_users.add(username)
						finally:
							client.events.unregister(TransferRemovedEvent, on_removed_t)
							client.events.unregister(TransferProgressEvent, on_progress_t)

					# Main loop: search and download perfect matches in parallel
					search_start = time.time()

					while not stop_event.is_set() and not download_success.is_set():
						# Check for perfect matches to try
						try:
							perfect = perfect_queue.get_nowait()
							pm_user, pm_file, pm_size, pm_ext, pm_bitrate, pm_sim = perfect

							if pm_user not in failed_users:
								print(f"\033[95m[DOWNLOAD] ⚡ Trying perfect match: {pm_file} from {pm_user}\033[0m")
								yield DownloadEvent(kind="status", message=f"Trying perfect match from {pm_user}")

								async for ev in try_download(pm_user, pm_file, pm_size, pm_ext):
									yield ev

								if download_success.is_set():
									print(f"\033[92m[DOWNLOAD] ✅ Perfect match downloaded successfully!\033[0m")
									# Stop search
									try:
										await client.searches.remove(search_request)
									except Exception:
										pass
									client.events.unregister(SearchResultEvent, on_result)
									client.events.unregister(SearchRequestRemovedEvent, on_removed)
									return
						except asyncio.QueueEmpty:
							pass

						await asyncio.sleep(0.1)

					# Cancel the search request
					try:
						await client.searches.remove(search_request)
					except Exception:
						pass

					client.events.unregister(SearchResultEvent, on_result)
					client.events.unregister(SearchRequestRemovedEvent, on_removed)

					# If download already succeeded, we're done
					if download_success.is_set():
						return

					if not collected:
						print(f"\033[91m[SEARCH] ❌ No results found for: '{query}'\033[0m")
						yield DownloadEvent(kind="error", message="no results")
						return

					# Log how many results we collected
					unique_users_collected = len(set(x[0] for x in collected))
					print(f"\033[92m[SEARCH] ✅ Search completed: Found {len(collected)} file(s) from {unique_users_collected} unique user(s)\033[0m")

					# Print ALL raw results (skip MP3s below 192kbps)
					print(f"\033[96m{'='*80}\033[0m")
					print(f"\033[96m[SEARCH] 📋 ALL RAW RESULTS ({len(collected)} files):\033[0m")
					print(f"\033[96m{'='*80}\033[0m")
					displayed_count = 0
					for i, (username, filename, fsize, ext, bitrate) in enumerate(collected, 1):
						if ext.lower() == "mp3":
							br = bitrate if bitrate > 0 else _infer_mp3_bitrate_from_name(filename)
							if br == 0:
								if fsize >= 9_000_000:
									br = 320
								elif fsize >= 5_500_000:
									br = 192
								else:
									br = 128
							if br < 192:
								continue
						displayed_count += 1
						size_mb = fsize / (1024 * 1024)
						bitrate_str = f" | Bitrate: {bitrate}kbps" if bitrate > 0 else ""
						print(f"\033[93m[{displayed_count}] User: {username}\033[0m")
						print(f"    \033[97mFile: {filename}\033[0m")
						print(f"    \033[94mSize: {size_mb:.2f} MB | Ext: {ext}{bitrate_str}\033[0m")
					print(f"\033[96m{'='*80}\033[0m")

					yield DownloadEvent(kind="status", message=f"No perfect match found. Selecting best from {len(collected)} files...")

					# No perfect match succeeded - process all collected results and pick best
					# Tuple: (username, filename, size, ext, similarity, bitrate)
					with_scores = [(
						username,
						filename,
						size,
						ext,
						_similarity(_remove_track_number_prefix(_basename_without_ext(filename), query), target),
						bitrate,
					) for (username, filename, size, ext, bitrate) in collected]

					# Show similarity distribution
					if with_scores:
						max_sim = max(x[4] for x in with_scores)
						min_sim = min(x[4] for x in with_scores)
						yield DownloadEvent(kind="status", message=f"Similarity range: {min_sim:.2f} - {max_sim:.2f}")

					max_sim = max(x[4] for x in with_scores) if with_scores else 0
					# Be more lenient with similarity - use 0.15 instead of 0.05 to include more results
					filtered = [x for x in with_scores if x[4] >= max_sim - 0.15]
					filtered.sort(key=lambda x: _quality_tuple(x[1], x[2], x[3], x[4], preferred_format))
					filtered = list(reversed(filtered))

					# Deduplicate by username: keep only the best candidate per user
					seen_users = set()
					dedup = []
					for x in filtered:
						user = x[0]
						if user in seen_users:
							continue
						seen_users.add(user)
						dedup.append(x)
					# Fallback: if too few unique users, allow additional files per same users
					if len(dedup) < 3:
						for x in filtered:
							if x in dedup:
								continue
							dedup.append(x)
							if len(dedup) >= 5:
								break
					filtered = dedup

					# Skip users that already failed during perfect match attempts
					filtered = [x for x in filtered if x[0] not in failed_users ]

					# Apply high-quality gate for initial selection
					hq = [x for x in filtered if _is_high_quality(x[1], x[2], x[3], x[4])]
					if hq:
						if preferred_format and preferred_format.lower() == "mp3":
							mp3_320_hq = [x for x in hq if x[3].lower() == "mp3" and _get_effective_bitrate(x[1], x[2], x[5]) >= 320]
							if mp3_320_hq:
								candidates = mp3_320_hq
								yield DownloadEvent(kind="status", message=f"using {len(candidates)} MP3 320 HQ candidates")
							else:
								# No MP3 320 in HQ - search ALL results for MP3 320
								all_mp3_320 = [x for x in with_scores if x[3].lower() == "mp3" and _get_effective_bitrate(x[1], x[2], x[5]) >= 320 and x[2] >= 3_000_000 and x[0] not in failed_users ]
								if all_mp3_320:
									all_mp3_320.sort(key=lambda x: (x[4], x[2]), reverse=True)
									candidates = all_mp3_320
									yield DownloadEvent(kind="status", message=f"using {len(candidates)} MP3 320 candidates (from all results)")
								else:
									# No MP3 320 found anywhere - use HQ with FLAC > WAV priority
									flac_hq = [x for x in hq if x[3].lower() == "flac"]
									wav_hq = [x for x in hq if x[3].lower() == "wav"]
									if flac_hq:
										flac_hq.sort(key=lambda x: (x[4], x[2]), reverse=True)
										candidates = flac_hq
										yield DownloadEvent(kind="status", message=f"no MP3 320 found; using {len(candidates)} FLAC HQ candidates")
									elif wav_hq:
										wav_hq.sort(key=lambda x: (x[4], x[2]), reverse=True)
										candidates = wav_hq
										yield DownloadEvent(kind="status", message=f"no MP3 320/FLAC found; using {len(candidates)} WAV HQ candidates")
									else:
										candidates = hq
										yield DownloadEvent(kind="status", message=f"no MP3 320 found; using {len(hq)} HQ candidates")
						else:
							# FLAC preferred - prioritize FLAC from HQ, then MP3 320, then WAV
							flac_hq = [x for x in hq if x[3].lower() == "flac"]
							if flac_hq:
								flac_hq.sort(key=lambda x: (x[4], x[2]), reverse=True)
								candidates = flac_hq
								yield DownloadEvent(kind="status", message=f"using {len(candidates)} FLAC HQ candidates")
							else:
								# No FLAC in HQ - search ALL results for FLAC
								all_flac = [x for x in with_scores if x[3].lower() == "flac" and x[2] >= 3_000_000 and x[0] not in failed_users ]
								if all_flac:
									all_flac.sort(key=lambda x: (x[4], x[2]), reverse=True)
									candidates = all_flac
									yield DownloadEvent(kind="status", message=f"using {len(candidates)} FLAC candidates (from all results)")
								else:
									# No FLAC found - try MP3 320
									mp3_320_hq = [x for x in hq if x[3].lower() == "mp3" and _get_effective_bitrate(x[1], x[2], x[5]) >= 320]
									if mp3_320_hq:
										candidates = mp3_320_hq
										yield DownloadEvent(kind="status", message=f"no FLAC found; using {len(candidates)} MP3 320 HQ candidates")
									else:
										all_mp3_320 = [x for x in with_scores if x[3].lower() == "mp3" and _get_effective_bitrate(x[1], x[2], x[5]) >= 320 and x[2] >= 3_000_000 and x[0] not in failed_users ]
										if all_mp3_320:
											all_mp3_320.sort(key=lambda x: (x[4], x[2]), reverse=True)
											candidates = all_mp3_320
											yield DownloadEvent(kind="status", message=f"no FLAC found; using {len(candidates)} MP3 320 candidates")
										else:
											# Try WAV
											wav_hq = [x for x in hq if x[3].lower() == "wav"]
											if wav_hq:
												wav_hq.sort(key=lambda x: (x[4], x[2]), reverse=True)
												candidates = wav_hq
												yield DownloadEvent(kind="status", message=f"no FLAC/MP3 320 found; using {len(candidates)} WAV HQ candidates")
											else:
												candidates = hq
												yield DownloadEvent(kind="status", message=f"using {len(hq)} HQ candidates")
					else:
						if preferred_format and preferred_format.lower() == "mp3":
							# Search for MP3 320 in ALL results (with_scores), not just similarity-filtered
							# This is important when artist name is in folder path, not filename
							all_mp3_320 = [x for x in with_scores if x[3].lower() == "mp3" and _get_effective_bitrate(x[1], x[2], x[5]) >= 320 and x[2] >= 3_000_000 and x[0] not in failed_users ]
							if all_mp3_320:
								# Sort by similarity (highest first), then by size
								all_mp3_320.sort(key=lambda x: (x[4], x[2]), reverse=True)
								candidates = all_mp3_320
								yield DownloadEvent(kind="status", message=f"using {len(candidates)} MP3 320 candidates (from all results)")
							else:
								# No MP3 320 found anywhere - try FLAC from all results
								all_flac = [x for x in with_scores if x[3].lower() == "flac" and x[2] >= 3_000_000 and x[0] not in failed_users ]
								if all_flac:
									all_flac.sort(key=lambda x: (x[4], x[2]), reverse=True)
									candidates = all_flac
									yield DownloadEvent(kind="status", message=f"no MP3 320 found; using {len(candidates)} FLAC candidates")
								else:
									# Try WAV from all results
									all_wav = [x for x in with_scores if x[3].lower() == "wav" and x[2] >= 3_000_000 and x[0] not in failed_users ]
									if all_wav:
										all_wav.sort(key=lambda x: (x[4], x[2]), reverse=True)
										candidates = all_wav
										yield DownloadEvent(kind="status", message=f"no MP3 320/FLAC found; using {len(candidates)} WAV candidates")
									else:
										# Try MP3 256/192 from all results
										all_mp3_lower = [x for x in with_scores if x[3].lower() == "mp3" and _get_effective_bitrate(x[1], x[2], x[5]) >= 192 and x[2] >= 3_000_000 and x[0] not in failed_users ]
										if all_mp3_lower:
											all_mp3_lower.sort(key=lambda x: (_get_effective_bitrate(x[1], x[2], x[5]), x[4], x[2]), reverse=True)
											candidates = all_mp3_lower
											yield DownloadEvent(kind="status", message=f"using {len(candidates)} MP3 256/192 candidates")
										else:
											fallback = [x for x in with_scores if x[2] >= 3_000_000 and x[0] not in failed_users ]
											candidates = fallback or filtered
											yield DownloadEvent(kind="status", message=f"using {len(candidates)} fallback candidates")
						else:
							# FLAC preferred: FLAC > MP3 320 > WAV > MP3 256 > MP3 192
							all_flac = [x for x in with_scores if x[3].lower() == "flac" and x[2] >= 3_000_000 and x[0] not in failed_users ]
							if all_flac:
								all_flac.sort(key=lambda x: (x[4], x[2]), reverse=True)
								candidates = all_flac
								yield DownloadEvent(kind="status", message=f"using {len(candidates)} FLAC candidates")
							else:
								all_mp3_320 = [x for x in with_scores if x[3].lower() == "mp3" and _get_effective_bitrate(x[1], x[2], x[5]) >= 320 and x[2] >= 3_000_000 and x[0] not in failed_users ]
								if all_mp3_320:
									all_mp3_320.sort(key=lambda x: (x[4], x[2]), reverse=True)
									candidates = all_mp3_320
									yield DownloadEvent(kind="status", message=f"no FLAC found; using {len(candidates)} MP3 320 candidates")
								else:
									all_wav = [x for x in with_scores if x[3].lower() == "wav" and x[2] >= 3_000_000 and x[0] not in failed_users ]
									if all_wav:
										all_wav.sort(key=lambda x: (x[4], x[2]), reverse=True)
										candidates = all_wav
										yield DownloadEvent(kind="status", message=f"no FLAC/MP3 320 found; using {len(candidates)} WAV candidates")
									else:
										all_mp3_lower = [x for x in with_scores if x[3].lower() == "mp3" and _get_effective_bitrate(x[1], x[2], x[5]) >= 192 and x[2] >= 3_000_000 and x[0] not in failed_users ]
										if all_mp3_lower:
											all_mp3_lower.sort(key=lambda x: (_get_effective_bitrate(x[1], x[2], x[5]), x[4], x[2]), reverse=True)
											candidates = all_mp3_lower
											yield DownloadEvent(kind="status", message=f"using {len(candidates)} MP3 256/192 candidates")
										else:
											fallback = [x for x in with_scores if x[2] >= 3_000_000 and x[0] not in failed_users ]
											candidates = fallback or filtered
											yield DownloadEvent(kind="status", message=f"using {len(candidates)} fallback candidates")

					if not candidates:
						yield DownloadEvent(kind="error", message="No suitable candidates found after filtering")
						return

					# Send only basenames (no paths) so client can compare e.g. "Mau P - neck (Extended Mix).flac" with query
					candidate_filenames = [_basename_from_path(x[1]) for x in candidates]
					yield DownloadEvent(kind="files_list", files_list=candidate_filenames, message=f"Found {len(candidate_filenames)} candidate files")

					# Wait for client confirmation before starting download
					if confirmation_event:
						yield DownloadEvent(kind="status", message="Waiting for client confirmation...")
						try:
							await asyncio.wait_for(confirmation_event.wait(), timeout=30.0)
							if not confirmation_event.is_set():
								yield DownloadEvent(kind="error", message="Confirmation timeout")
								return
						except asyncio.TimeoutError:
							yield DownloadEvent(kind="error", message="Confirmation timeout")
							return

					# Try each candidate using the same try_download function
					for idx, (username, filename, size, ext, sim, bitrate) in enumerate(candidates, 1):
						if username in failed_users:
							continue

						yield DownloadEvent(kind="status", message=f"candidate #{idx}: {username} | {ext} | {size}")
						async for ev in try_download(username, filename, size, ext):
							yield ev

						if download_success.is_set():
							print(f"\033[92m[DOWNLOAD] ✅ Downloaded successfully!\033[0m")
							return

					# All candidates failed
					yield DownloadEvent(kind="error", message=f"All candidates failed. No more options available.")
					return

				break  # Successfully connected and completed
			except Exception as e:
				if "listening port" in str(e).lower() and attempt < max_retries - 1:
					yield DownloadEvent(kind="status", message=f"Connection attempt {attempt + 1}/{max_retries} failed, retrying...")
					await asyncio.sleep(2)
					continue
				else:
					yield DownloadEvent(kind="error", message=f"Failed to connect: {e}")
					return

	async def download(self, query: str, preferred_format: Optional[str] = None, confirmation_event: Optional[asyncio.Event] = None) -> AsyncIterator[DownloadEvent]:
		async for ev in self._download_one(query, preferred_format, confirmation_event):
			yield ev
