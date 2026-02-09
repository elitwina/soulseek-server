import asyncio
import os
from typing import AsyncIterator, Optional

from aioslsk.client import SoulSeekClient
from aioslsk.settings import Settings, CredentialsSettings
from aioslsk.network.network import ListeningConnectionErrorMode
from aioslsk.events import (
	SearchResultEvent,
	SearchRequestRemovedEvent,
)

from slsk_models import DownloadEvent
from slsk_utils import (
	ALLOWED_EXTS,
	MIN_SIZE_BYTES,
	_basename_without_ext,
	_similarity,
	_quality_tuple,
	_is_high_quality,
	_infer_mp3_bitrate_from_name,
)


async def perform_search(
	client: SoulSeekClient,
	query: str,
	search_timeout: int,
	preferred_format: Optional[str] = None,
	event_queue: Optional[asyncio.Queue[DownloadEvent]] = None
) -> tuple[list[tuple[str, str, int, str, float]], list[tuple[str, str, int, str, float]], list[tuple[str, str, int, str, float]]]:
	"""
	Perform a search on Soulseek and return filtered candidates.
	
	Returns:
		(candidates, fallback_candidates, with_scores)
	"""
	collected: list[tuple[str, str, int, str]] = []
	stop_event = asyncio.Event()
	
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

	print(f"[SEARCH] 🔍 Sending search request for: '{query}' (timeout: {search_timeout}s)")
	search_request = await client.searches.search(query)
	search_request_id = search_request.request_id if hasattr(search_request, 'request_id') else None
	print(f"[SEARCH] ✅ Search request sent (request_id: {search_request_id})")
	
	if event_queue:
		await event_queue.put(DownloadEvent(kind="status", message=f"searching '{query}' ({search_timeout}s)"))
	
	# Wait for search to complete or timeout
	search_timeout_reached = False
	search_start_time = asyncio.get_event_loop().time()
	while not stop_event.is_set():
		try:
			await asyncio.wait_for(stop_event.wait(), timeout=0.2)
			break
		except asyncio.TimeoutError:
			# Check if search timeout was reached
			elapsed = asyncio.get_event_loop().time() - search_start_time
			if elapsed >= search_timeout and not search_timeout_reached:
				search_timeout_reached = True
				print(f"[SEARCH] ⏱️  Search timeout reached ({search_timeout}s), stopping search")
				# Try to remove the search request if possible
				if search_request_id:
					try:
						await client.searches.remove(search_request_id)
						print(f"[SEARCH] 🗑️  Removed search request {search_request_id}")
					except Exception as e:
						print(f"[SEARCH] ⚠️  Could not remove search request: {e}")
				stop_event.set()
			pass
	
	client.events.unregister(SearchResultEvent, on_result)
	client.events.unregister(SearchRequestRemovedEvent, on_removed)

	if not collected:
		print(f"\033[91m[SEARCH] ❌ No results found for: '{query}'\033[0m")
		return [], [], []

	# Log how many results we collected
	unique_users_collected = len(set(x[0] for x in collected))
	print(f"\033[92m[SEARCH] ✅ Search completed: Found {len(collected)} file(s) from {unique_users_collected} unique user(s)\033[0m")

	# Print ALL raw results in color for debugging
	print(f"\033[96m{'='*80}\033[0m")
	print(f"\033[96m[SEARCH] 📋 ALL RAW RESULTS ({len(collected)} files):\033[0m")
	print(f"\033[96m{'='*80}\033[0m")
	for i, (username, filename, fsize, ext) in enumerate(collected, 1):
		size_mb = fsize / (1024 * 1024)
		print(f"\033[93m[{i}] User: {username}\033[0m")
		print(f"    \033[97mFile: {filename}\033[0m")
		print(f"    \033[94mSize: {size_mb:.2f} MB | Ext: {ext}\033[0m")
	print(f"\033[96m{'='*80}\033[0m")
	if event_queue:
		await event_queue.put(DownloadEvent(kind="status", message=f"Collected {len(collected)} files from {unique_users_collected} unique user(s)"))

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
		if event_queue:
			await event_queue.put(DownloadEvent(kind="status", message=f"Similarity range: {min_sim:.2f} - {max_sim:.2f}"))
	
	max_sim = max(x[4] for x in with_scores) if with_scores else 0
	# Be more lenient with similarity - use 0.30 to include more results (handles underscores, hyphens, etc.)
	similarity_threshold = max_sim - 0.30
	print(f"[SEARCH] 🎯 Filtering results (similarity threshold: {similarity_threshold:.2f})...")
	# Log all files before filtering to see what's being filtered out
	for x in with_scores:
		passed = x[4] >= similarity_threshold
		status = "✅ PASS" if passed else "❌ FILTERED"
		print(f"[SEARCH] {status}: user='{x[0]}', file='{x[1]}', size={x[2]}, ext={x[3]}, sim={x[4]:.2f} (threshold: {similarity_threshold:.2f})")
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
			print(f"[SEARCH] ⏭️  Skipping duplicate user '{user}' (file: {x[1]})")
			continue
		seen_users.add(user)
		dedup.append(x)
		print(f"[SEARCH] ✅ Added user '{user}' (file: {x[1]}, size: {x[2]}, ext: {x[3]}, sim: {x[4]:.2f})")
	# Fallback: if too few unique users, allow additional files per same users
	if len(dedup) < 3:
		print(f"[SEARCH] ⚠️  Only {len(dedup)} unique users, adding more files from same users...")
		for x in filtered:
			if x in dedup:
				continue
			dedup.append(x)
			print(f"[SEARCH] ✅ Added additional file from '{x[0]}' (file: {x[1]}, size: {x[2]}, ext: {x[3]}, sim: {x[4]:.2f})")
			if len(dedup) >= 5:
				break
	filtered = dedup
	print(f"[SEARCH] ✅ After deduplication: {len(filtered)} unique file(s) from {len(seen_users)} user(s)")
	# Log all users in filtered
	for x in filtered:
		print(f"[SEARCH] 📋 Candidate: user='{x[0]}', file='{x[1]}', size={x[2]}, ext={x[3]}, sim={x[4]:.2f}")

	# Apply high-quality gate for initial selection
	print(f"[SEARCH] 🎚️  Applying quality filter...")
	hq = [x for x in filtered if _is_high_quality(x[1], x[2], x[3], x[4])]
	print(f"[SEARCH] ✅ High-quality files: {len(hq)} out of {len(filtered)}")
	
	candidates = []
	fallback_candidates = []
	
	if hq:
		# If preferred_format is mp3, prioritize MP3 320 from HQ candidates
		if preferred_format and preferred_format.lower() == "mp3":
			mp3_320_hq = [x for x in hq if x[3].lower() == "mp3" and _infer_mp3_bitrate_from_name(x[1]) >= 320]
			if mp3_320_hq:
				candidates = mp3_320_hq
				if event_queue:
					await event_queue.put(DownloadEvent(kind="status", message=f"using {len(candidates)} MP3 320 HQ candidates (preferred format)"))
			else:
				# No MP3 320 in HQ, use FLAC/WAV from HQ, but prepare fallback to MP3 256/192
				candidates = hq
				if event_queue:
					await event_queue.put(DownloadEvent(kind="status", message=f"using {len(hq)} high-quality candidates (no MP3 320 found)"))
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
					if event_queue:
						await event_queue.put(DownloadEvent(kind="status", message=f"Added {len(fallback_mp3)} MP3 candidates as fallback ({len(fallback_mp3_known)} with known bitrate, {len(fallback_mp3_unknown)} unknown bitrate, {len(all_mp3s)} total MP3s)"))
				else:
					if event_queue:
						await event_queue.put(DownloadEvent(kind="status", message=f"No MP3 fallback found (found {len(all_mp3s)} total MP3s)"))
		else:
			candidates = hq
			if event_queue:
				await event_queue.put(DownloadEvent(kind="status", message=f"using {len(hq)} high-quality candidates"))
	else:
		# Fallback: if preferred_format is mp3, try in order: MP3 320 > FLAC > MP3 256/192
		if preferred_format and preferred_format.lower() == "mp3":
			# Try MP3 320 first
			mp3_320 = [x for x in filtered if x[3].lower() == "mp3" and _infer_mp3_bitrate_from_name(x[1]) >= 320 and x[2] >= 3_000_000]
			if mp3_320:
				candidates = mp3_320
				if event_queue:
					await event_queue.put(DownloadEvent(kind="status", message=f"no HQ found; using {len(candidates)} MP3 320 candidates (preferred format)"))
			else:
				# Try FLAC second
				flac_candidates = [x for x in filtered if x[3].lower() == "flac" and x[2] >= 3_000_000]
				if flac_candidates:
					candidates = flac_candidates
					if event_queue:
						await event_queue.put(DownloadEvent(kind="status", message=f"no MP3 320 found; using {len(candidates)} FLAC candidates"))
				else:
					# Try MP3 256/192 last
					mp3_lower = [x for x in filtered if x[3].lower() == "mp3" and _infer_mp3_bitrate_from_name(x[1]) >= 192 and x[2] >= 3_000_000]
					if mp3_lower:
						candidates = mp3_lower
						if event_queue:
							await event_queue.put(DownloadEvent(kind="status", message=f"no MP3 320/FLAC found; using {len(candidates)} MP3 256/192 candidates"))
					else:
						# Final fallback
						fallback = [x for x in filtered if x[2] >= 3_000_000]
						candidates = fallback or filtered
						if event_queue:
							await event_queue.put(DownloadEvent(kind="status", message=f"no preferred format found; using {len(candidates)} filtered candidates"))
		else:
			# Fallback: skip obviously low-quality (tiny/low-bitrate-looking) files
			fallback = [x for x in filtered if x[2] >= 3_000_000]
			candidates = fallback or filtered
			if event_queue:
				await event_queue.put(DownloadEvent(kind="status", message=f"no HQ found; using {len(candidates)} filtered candidates"))

	# Prepare fallback candidates (all filtered files, not just high-quality)
	# If high-quality candidates fail, we'll try the rest
	if hq and candidates == hq:
		# If we're using only high-quality, prepare fallback from filtered
		fallback_candidates = [x for x in filtered if x not in hq]
		if fallback_candidates:
			print(f"[SEARCH] 🔄 Prepared {len(fallback_candidates)} fallback candidate(s) in case high-quality fails")
			for x in fallback_candidates:
				print(f"[SEARCH] 📋 Fallback: user='{x[0]}', file='{x[1]}', size={x[2]}, ext={x[3]}, sim={x[4]:.2f}, HQ={_is_high_quality(x[1], x[2], x[3], x[4])}")
	else:
		# If we're not using only HQ, check if there are any filtered files not in candidates
		fallback_candidates = [x for x in filtered if x not in candidates]
		if fallback_candidates:
			print(f"[SEARCH] 🔄 Prepared {len(fallback_candidates)} fallback candidate(s) from filtered (not in primary candidates)")
			for x in fallback_candidates:
				print(f"[SEARCH] 📋 Fallback: user='{x[0]}', file='{x[1]}', size={x[2]}, ext={x[3]}, sim={x[4]:.2f}")

	return candidates, fallback_candidates, with_scores



