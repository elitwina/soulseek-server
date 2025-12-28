import asyncio
import os
import re
import aiohttp
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import AsyncIterator, Iterable, Optional

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


@dataclass
class DownloadEvent:
	kind: str  # 'status' | 'progress' | 'finished' | 'error' | 'started' | 'files_list'
	message: str = ""
	percent: Optional[int] = None
	path: Optional[str] = None
	files_list: Optional[list[str]] = None  # List of candidate file names for client to check


class SoulseekService:
	def __init__(self, username: str, password: str, download_dir: str, search_timeout: int = 10):
		self.username = username
		self.password = password
		self.download_dir = download_dir
		self.search_timeout = search_timeout

	async def _download_one(self, query: str, preferred_format: Optional[str] = None, confirmation_event: Optional[asyncio.Event] = None) -> AsyncIterator[DownloadEvent]:
		os.makedirs(self.download_dir, exist_ok=True)
		settings = Settings(credentials=CredentialsSettings(username=self.username, password=self.password))
		settings.shares.download = self.download_dir
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

		collected: list[tuple[str, str, int, str]] = []
		stop_event = asyncio.Event()
		event_queue: asyncio.Queue[DownloadEvent] = asyncio.Queue(maxsize=1000)

		# Try to connect with retry logic for listening port issues
		max_retries = 3
		for attempt in range(max_retries):
			try:
				print(f"[SEARCH] 🔌 Connecting to Soulseek (attempt {attempt + 1}/{max_retries})...")
				async with SoulSeekClient(settings=settings) as client:
					print(f"[SEARCH] 🔐 Logging in to Soulseek...")
					await client.login()
					print(f"[SEARCH] ✅ Successfully logged in to Soulseek")

					search_request_id = None
					search_started = False

					async def on_result(event: SearchResultEvent):
						nonlocal search_started
						if not search_started:
							search_started = True
							print(f"[SEARCH] 📡 Search request active, receiving results...")
						
						result_count = 0
						for file in list(event.result.shared_items) + list(event.result.locked_results):
							ext = (file.extension or os.path.splitext(file.filename)[1][1:]).lower()
							if ext not in ALLOWED_EXTS:
								continue
							fsize = int(file.filesize)
							if fsize < MIN_SIZE_BYTES:
								continue
							collected.append((event.result.username, file.filename, fsize, ext))
							result_count += 1
						
						if result_count > 0:
							print(f"[SEARCH] 📥 Received {result_count} file(s) from user '{event.result.username}' (total collected: {len(collected)})")

					async def on_removed(event: SearchRequestRemovedEvent):
						nonlocal search_request_id
						# Check if this is our search request
						if search_request_id and event.request_id == search_request_id:
							print(f"[SEARCH] ⚠️  Search request was removed by server (request_id: {search_request_id})")
							print(f"[SEARCH] ⚠️  This usually means: session expired, timeout, or server closed the search")
							if not collected:
								print(f"[SEARCH] ❌ No results collected before search was removed")
							else:
								print(f"[SEARCH] ✅ Collected {len(collected)} results before search was removed")
						stop_event.set()

					client.events.register(SearchResultEvent, on_result)
					client.events.register(SearchRequestRemovedEvent, on_removed)

					print(f"[SEARCH] 🔍 Sending search request for: '{query}' (timeout: {self.search_timeout}s)")
					search_request = await client.searches.search(query)
					search_request_id = search_request.request_id if hasattr(search_request, 'request_id') else None
					print(f"[SEARCH] ✅ Search request sent (request_id: {search_request_id})")
					
					await event_queue.put(DownloadEvent(kind="status", message=f"searching '{query}' ({self.search_timeout}s)"))
					
					# Yield events in real-time while waiting for search to end
					search_timeout_reached = False
					search_start_time = asyncio.get_event_loop().time()
					while not stop_event.is_set():
						try:
							ev = await asyncio.wait_for(event_queue.get(), timeout=0.2)
							yield ev
						except asyncio.TimeoutError:
							# Check if search timeout was reached
							elapsed = asyncio.get_event_loop().time() - search_start_time
							if elapsed >= self.search_timeout and not search_timeout_reached:
								search_timeout_reached = True
								print(f"[SEARCH] ⏱️  Search timeout reached ({self.search_timeout}s), stopping search")
								# Try to remove the search request if possible
								if search_request_id:
									try:
										await client.searches.remove(search_request_id)
										print(f"[SEARCH] 🗑️  Removed search request {search_request_id}")
									except Exception as e:
										print(f"[SEARCH] ⚠️  Could not remove search request: {e}")
								stop_event.set()
							pass
					
					# Drain remaining queued events
					while not event_queue.empty():
						ev = await event_queue.get()
						yield ev
					
					client.events.unregister(SearchResultEvent, on_result)
					client.events.unregister(SearchRequestRemovedEvent, on_removed)

					if not collected:
						print(f"[SEARCH] ❌ No results found for: '{query}'")
						yield DownloadEvent(kind="error", message="no results")
						return

					# Log how many results we collected
					unique_users_collected = len(set(x[0] for x in collected))
					print(f"[SEARCH] ✅ Search completed: Found {len(collected)} file(s) from {unique_users_collected} unique user(s)")
					yield DownloadEvent(kind="status", message=f"Collected {len(collected)} files from {unique_users_collected} unique user(s)")

					target = _basename_without_ext(query)
					print(f"[SEARCH] 🔍 Calculating similarity scores for {len(collected)} files...")
					with_scores = [(
						username,
						filename,
						size,
						ext,
						_similarity(_basename_without_ext(filename), target),
					) for (username, filename, size, ext) in collected]
					
					# Show similarity distribution
					if with_scores:
						max_sim = max(x[4] for x in with_scores)
						min_sim = min(x[4] for x in with_scores)
						print(f"[SEARCH] 📊 Similarity range: {min_sim:.2f} - {max_sim:.2f}")
						yield DownloadEvent(kind="status", message=f"Similarity range: {min_sim:.2f} - {max_sim:.2f}")
					
					max_sim = max(x[4] for x in with_scores) if with_scores else 0
					# Be more lenient with similarity - use 0.30 to include more results (handles underscores, hyphens, etc.)
					similarity_threshold = max_sim - 0.30
					print(f"[SEARCH] 🎯 Filtering results (similarity threshold: {similarity_threshold:.2f})...")
					filtered = [x for x in with_scores if x[4] >= similarity_threshold]
					print(f"[SEARCH] ✅ After similarity filtering: {len(filtered)} file(s) remaining (from {len(with_scores)} total)")
					
					filtered.sort(key=lambda x: _quality_tuple(x[1], x[2], x[3], x[4], preferred_format))
					filtered = list(reversed(filtered))

					# Deduplicate by username: keep only the best candidate per user
					print(f"[SEARCH] 🔄 Deduplicating by username...")
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
					print(f"[SEARCH] ✅ After deduplication: {len(filtered)} unique file(s) from {len(seen_users)} user(s)")

					# Apply high-quality gate for initial selection
					print(f"[SEARCH] 🎚️  Applying quality filter...")
					hq = [x for x in filtered if _is_high_quality(x[1], x[2], x[3], x[4])]
					print(f"[SEARCH] ✅ High-quality files: {len(hq)} out of {len(filtered)}")
					if hq:
						# If preferred_format is mp3, prioritize MP3 320 from HQ candidates
						if preferred_format and preferred_format.lower() == "mp3":
							mp3_320_hq = [x for x in hq if x[3].lower() == "mp3" and _infer_mp3_bitrate_from_name(x[1]) >= 320]
							if mp3_320_hq:
								candidates = mp3_320_hq
								yield DownloadEvent(kind="status", message=f"using {len(candidates)} MP3 320 HQ candidates (preferred format)")
							else:
								# No MP3 320 in HQ, use FLAC/WAV from HQ, but prepare fallback to MP3 256/192
								candidates = hq
								yield DownloadEvent(kind="status", message=f"using {len(hq)} high-quality candidates (no MP3 320 found)")
								# Store filtered for fallback if HQ fails - check all collected files, not just filtered
								# Use with_scores (all collected files) to find MP3 256/192 even if they didn't pass similarity filter
								all_mp3s = [x for x in with_scores if x[3].lower() == "mp3" and x[2] >= 3_000_000 and x not in hq]
								# Prioritize MP3s with known bitrate >= 192, but also include others with reasonable size
								fallback_mp3_known = [x for x in all_mp3s if _infer_mp3_bitrate_from_name(x[1]) >= 192]
								fallback_mp3_unknown = [x for x in all_mp3s if _infer_mp3_bitrate_from_name(x[1]) == 0 and x[2] >= 4_000_000]  # Unknown bitrate but reasonable size
								fallback_mp3 = fallback_mp3_known + fallback_mp3_unknown
								if fallback_mp3:
									# Sort by bitrate (known first) and similarity
									fallback_mp3.sort(key=lambda x: (_infer_mp3_bitrate_from_name(x[1]) if _infer_mp3_bitrate_from_name(x[1]) > 0 else 0, x[4]), reverse=True)
									# Append MP3 256/192 as fallback after HQ candidates
									candidates = candidates + fallback_mp3
									yield DownloadEvent(kind="status", message=f"Added {len(fallback_mp3)} MP3 candidates as fallback ({len(fallback_mp3_known)} with known bitrate, {len(fallback_mp3_unknown)} unknown bitrate, {len(all_mp3s)} total MP3s)")
								else:
									yield DownloadEvent(kind="status", message=f"No MP3 fallback found (found {len(all_mp3s)} total MP3s)")
						else:
							candidates = hq
							yield DownloadEvent(kind="status", message=f"using {len(hq)} high-quality candidates")
					else:
						# Fallback: if preferred_format is mp3, try in order: MP3 320 > FLAC > MP3 256/192
						if preferred_format and preferred_format.lower() == "mp3":
							# Try MP3 320 first
							mp3_320 = [x for x in filtered if x[3].lower() == "mp3" and _infer_mp3_bitrate_from_name(x[1]) >= 320 and x[2] >= 3_000_000]
							if mp3_320:
								candidates = mp3_320
								yield DownloadEvent(kind="status", message=f"no HQ found; using {len(candidates)} MP3 320 candidates (preferred format)")
							else:
								# Try FLAC second
								flac_candidates = [x for x in filtered if x[3].lower() == "flac" and x[2] >= 3_000_000]
								if flac_candidates:
									candidates = flac_candidates
									yield DownloadEvent(kind="status", message=f"no MP3 320 found; using {len(candidates)} FLAC candidates")
								else:
									# Try MP3 256/192 last
									mp3_lower = [x for x in filtered if x[3].lower() == "mp3" and _infer_mp3_bitrate_from_name(x[1]) >= 192 and x[2] >= 3_000_000]
									if mp3_lower:
										candidates = mp3_lower
										yield DownloadEvent(kind="status", message=f"no MP3 320/FLAC found; using {len(candidates)} MP3 256/192 candidates")
									else:
										# Final fallback
										fallback = [x for x in filtered if x[2] >= 3_000_000]
										candidates = fallback or filtered
										yield DownloadEvent(kind="status", message=f"no preferred format found; using {len(candidates)} filtered candidates")
						else:
							# Fallback: skip obviously low-quality (tiny/low-bitrate-looking) files
							fallback = [x for x in filtered if x[2] >= 3_000_000]
							candidates = fallback or filtered
							yield DownloadEvent(kind="status", message=f"no HQ found; using {len(candidates)} filtered candidates")

					# Prepare fallback candidates (all filtered files, not just high-quality)
					# If high-quality candidates fail, we'll try the rest
					fallback_candidates = []
					if hq and candidates == hq:
						# If we're using only high-quality, prepare fallback from filtered
						fallback_candidates = [x for x in filtered if x not in hq]
						if fallback_candidates:
							print(f"[SEARCH] 🔄 Prepared {len(fallback_candidates)} fallback candidate(s) in case high-quality fails")
					
					# Send list of candidate file names to client for existence check
					# Include both primary candidates and fallback candidates
					all_candidates_for_client = candidates + fallback_candidates
					candidate_filenames = [x[1] for x in all_candidates_for_client]  # Extract just the filenames
					print(f"[SEARCH] 📋 Sending {len(candidate_filenames)} candidate file(s) to client for verification ({len(candidates)} primary, {len(fallback_candidates)} fallback)")
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

					# Track users that have already failed to avoid retrying them
					failed_users = set()
					
					# Combine primary candidates with fallback candidates
					all_candidates_to_try = candidates + fallback_candidates
					
					# Check if all candidates are from the same user
					unique_users_in_candidates = set(x[0] for x in all_candidates_to_try)
					if len(unique_users_in_candidates) == 1:
						only_user = list(unique_users_in_candidates)[0]
						yield DownloadEvent(kind="status", message=f"Warning: All {len(all_candidates_to_try)} candidates are from the same user ({only_user}). If this user fails, no alternatives available.")
					else:
						print(f"[DOWNLOAD] ✅ Have candidates from {len(unique_users_in_candidates)} unique user(s) to try")
					
					# Filter out already failed users
					remaining_candidates = [c for c in all_candidates_to_try if c[0] not in failed_users]
					
					# Split into batches of 5 for parallel download attempts
					BATCH_SIZE = 5
					candidates_tried = 0
					finished_success = False
					
					# Process candidates in batches
					for batch_start in range(0, len(remaining_candidates), BATCH_SIZE):
						if finished_success:
							break
						
						batch = remaining_candidates[batch_start:batch_start + BATCH_SIZE]
						batch_num = (batch_start // BATCH_SIZE) + 1
						total_batches = (len(remaining_candidates) + BATCH_SIZE - 1) // BATCH_SIZE
						
						print(f"[DOWNLOAD] 📦 Batch {batch_num}/{total_batches}: Attempting {len(batch)} candidate(s) in parallel")
						yield DownloadEvent(kind="status", message=f"Batch {batch_num}/{total_batches}: Trying {len(batch)} candidate(s) in parallel")
						
						# Create download tasks for this batch
						download_tasks = []
						task_info = {}  # Map task to (username, filename, idx)
						
						for idx, (username, filename, size, ext, sim) in enumerate(batch, batch_start + 1):
							candidates_tried += 1
							print(f"[DOWNLOAD] 🔄 Candidate #{idx}: {username} | {ext} | {size} bytes")
							
							async def download_single_candidate(candidate_data, client_ref):
								"""Download a single candidate and return events via queue"""
								nonlocal finished_success
								c_username, c_filename, c_size, c_ext, c_sim, c_idx = candidate_data
								
								# Skip if another download already succeeded
								if finished_success:
									return None, None, None, False
								
								progress_started = asyncio.Event()
								complete_or_removed = asyncio.Event()
								last_percent: Optional[int] = None
								finished = False
								started_sent = False
								c_finished_success = False
								queued_notified = False
								
								# Create a queue for download events
								download_queue: asyncio.Queue[DownloadEvent] = asyncio.Queue(maxsize=1000)
								transfer_obj = None
								
								async def _finish_if_needed(transfer, curr_bytes: int):
									nonlocal last_percent, finished, c_finished_success, c_username
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
										await download_queue.put(DownloadEvent(kind="progress", percent=100, message="100%"))
										c_finished_success = True
									if (last_percent or 0) >= 100 or local_ok:
										c_finished_success = True
									if not finished:
										finished = True
										if c_finished_success:
											await download_queue.put(DownloadEvent(kind="finished", path=transfer.local_path or ""))
										else:
											failed_users.add(c_username)
											await download_queue.put(DownloadEvent(kind="status", message=f"failed from {c_username}"))
										complete_or_removed.set()

								async def on_removed_t(event: TransferRemovedEvent):
									if event.transfer.username == c_username and event.transfer.remote_path == c_filename:
										await _finish_if_needed(event.transfer, event.transfer.bytes_transfered)

								async def on_progress_t(event: TransferProgressEvent):
									nonlocal started_sent, last_percent, queued_notified
									for transfer, prev, curr in event.updates:
										if transfer.username != c_username or transfer.remote_path != c_filename:
											continue
										fs = transfer.filesize or 0
										if curr.state == TransferState.State.QUEUED and not queued_notified:
											queued_notified = True
											await download_queue.put(DownloadEvent(kind="status", message=f"{c_username} queued"))
										if (curr.bytes_transfered > 0 or curr.state == TransferState.State.DOWNLOADING) and not progress_started.is_set():
											progress_started.set()
											if not started_sent:
												started_sent = True
												print(f"[DOWNLOAD] ✅ Candidate #{c_idx} ({c_username}) started downloading")
												await download_queue.put(DownloadEvent(kind="started", path=transfer.local_path or ""))
										if fs > 0 and curr.bytes_transfered >= 0:
											percent = int((curr.bytes_transfered / fs) * 100)
											percent = max(1, min(100, percent))
											prev_p = 0 if last_percent is None else last_percent
											if percent > prev_p:
												for p in range(prev_p + 1, percent + 1):
													await download_queue.put(DownloadEvent(kind="progress", percent=p, message=f"{p}%"))
												last_percent = percent
										if curr.state in (TransferState.State.COMPLETE, TransferState.State.INCOMPLETE, TransferState.State.ABORTED, TransferState.State.FAILED):
											finished_success_local = (curr.state == TransferState.State.COMPLETE) or (fs > 0 and curr.bytes_transfered >= fs)
											if finished_success_local:
												c_finished_success = True
											progress_started.set()
											await _finish_if_needed(transfer, curr.bytes_transfered)

								client_ref.events.register(TransferRemovedEvent, on_removed_t)
								client_ref.events.register(TransferProgressEvent, on_progress_t)

								try:
									transfer_obj = await client_ref.transfers.download(c_username, c_filename)
								except Exception as download_error:
									error_str = str(download_error)
									error_type = type(download_error).__name__
									if 'PeerConnectionError' in error_type or 'peer' in error_str.lower() or 'connection' in error_str.lower():
										client_ref.events.unregister(TransferRemovedEvent, on_removed_t)
										client_ref.events.unregister(TransferProgressEvent, on_progress_t)
										failed_users.add(c_username)
										print(f"[DOWNLOAD] ❌ Candidate #{c_idx} ({c_username}): Connection failed")
										return None, None, None, False
									else:
										raise
								
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
										await client_ref.transfers.abort(transfer_obj)
										client_ref.events.unregister(TransferRemovedEvent, on_removed_t)
										client_ref.events.unregister(TransferProgressEvent, on_progress_t)
										failed_users.add(c_username)
										print(f"[DOWNLOAD] ⏱️  Candidate #{c_idx} ({c_username}): Stuck in queue, aborting")
										return None, None, None, False
									
									if not progress_started.is_set():
										await client_ref.transfers.abort(transfer_obj)
										client_ref.events.unregister(TransferRemovedEvent, on_removed_t)
										client_ref.events.unregister(TransferProgressEvent, on_progress_t)
										failed_users.add(c_username)
										print(f"[DOWNLOAD] ⏱️  Candidate #{c_idx} ({c_username}): Timeout, aborting")
										return None, None, None, False
								except asyncio.TimeoutError:
									await client_ref.transfers.abort(transfer_obj)
									client_ref.events.unregister(TransferRemovedEvent, on_removed_t)
									client_ref.events.unregister(TransferProgressEvent, on_progress_t)
									failed_users.add(c_username)
									print(f"[DOWNLOAD] ⏱️  Candidate #{c_idx} ({c_username}): Timeout exception, aborting")
									return None, None, None, False
								
								# Download started successfully - return the queue, transfer, and progress event
								return download_queue, transfer_obj, progress_started, c_finished_success
							
							# Create task for this candidate
							task = asyncio.create_task(download_single_candidate((username, filename, size, ext, sim, idx), client))
							download_tasks.append(task)
							task_info[task] = (username, filename, idx)
						
						# Wait for first successful download or all to fail
						successful_queue = None
						successful_transfer = None
						successful_progress_event = None
						
						# Get progress events from all tasks
						progress_events = []
						task_results = {}
						
						# Wait for all tasks to complete their initial setup (download initiation)
						# Then wait for one to start downloading
						try:
							# First, wait for all tasks to initiate downloads (or fail)
							done, pending = await asyncio.wait(download_tasks, timeout=15.0, return_when=asyncio.ALL_COMPLETED)
							
							# Collect results
							for task in download_tasks:
								try:
									queue, transfer, progress_evt, success = await task
									task_results[task] = (queue, transfer, progress_evt, success)
									if progress_evt is not None:
										progress_events.append(progress_evt)
								except Exception as e:
									print(f"[DOWNLOAD] ⚠️  Task error: {e}")
							
							# If we have progress events, wait for one to start
							if progress_events:
								print(f"[DOWNLOAD] ⏳ Waiting for one of {len(progress_events)} candidate(s) to start downloading...")
								done_progress, pending_progress = await asyncio.wait(
									[asyncio.create_task(ev.wait()) for ev in progress_events],
									timeout=15.0,
									return_when=asyncio.FIRST_COMPLETED
								)
								
								# Find which one started
								for task, (queue, transfer, progress_evt, success) in task_results.items():
									if progress_evt in done_progress and queue is not None and transfer is not None:
										successful_queue = queue
										successful_transfer = transfer
										successful_progress_event = progress_evt
										print(f"[DOWNLOAD] 🎯 Candidate #{task_info[task][2]} ({task_info[task][0]}) started - aborting others")
										
										# Abort all other transfers
										for other_task, (other_queue, other_transfer, other_progress, other_success) in task_results.items():
											if other_task != task and other_transfer is not None:
												try:
													await client.transfers.abort(other_transfer)
													print(f"[DOWNLOAD] 🛑 Aborted candidate #{task_info[other_task][2]}")
												except Exception as abort_err:
													print(f"[DOWNLOAD] ⚠️  Failed to abort candidate #{task_info[other_task][2]}: {abort_err}")
										
										# Stream events from successful download
										while True:
											try:
												ev = await asyncio.wait_for(successful_queue.get(), timeout=0.2)
												yield ev
												if ev.kind == "finished":
													finished_success = True
													print(f"[DOWNLOAD] ✅ Download completed successfully")
													break
												if ev.kind == "status" and "failed" in ev.message:
													print(f"[DOWNLOAD] ❌ Download failed")
													break
											except asyncio.TimeoutError:
												# Check if download is still active
												if successful_progress_event.is_set():
													continue
												else:
													break
										
										# Drain remaining events
										while not successful_queue.empty():
											ev = await successful_queue.get()
											yield ev
										
										if finished_success:
											break
							else:
								# No progress events - all failed
								print(f"[DOWNLOAD] ❌ Batch {batch_num}: All candidates failed to start")
						except Exception as e:
							print(f"[DOWNLOAD] ⚠️  Batch {batch_num} error: {e}")
							import traceback
							traceback.print_exc()
						
						# If this batch didn't succeed, continue to next batch
						if not finished_success:
							print(f"[DOWNLOAD] ❌ Batch {batch_num}: No successful download, trying next batch")
					
					# If we've tried all candidates and all failed
					if candidates_tried > 0 and len(failed_users) >= len(unique_users_in_candidates) and not finished_success:
						# Only send event to client, don't print (client will handle logging)
						yield DownloadEvent(kind="error", message=f"All {len(unique_users_in_candidates)} unique user(s) failed. Tried {candidates_tried} candidate(s) total. No more options available from search results.")
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
