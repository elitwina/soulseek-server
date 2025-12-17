import asyncio
import os
import re
import logging
from difflib import SequenceMatcher
from typing import Iterable, Optional

from aioslsk.client import SoulSeekClient
from aioslsk.settings import Settings, CredentialsSettings
from aioslsk.events import (
	SearchResultEvent,
	SearchRequestRemovedEvent,
	TransferAddedEvent,
	TransferProgressEvent,
	TransferRemovedEvent,
)
from aioslsk.transfer.manager import TransferManager, TransferState
from aioslsk.transfer.model import TransferProgressSnapshot

# Suppress noisy logs from aioslsk
logging.basicConfig(level=logging.ERROR)
for name in (
	"aioslsk",
	"aioslsk.client",
	"aioslsk.network",
	"aioslsk.peer",
	"aioslsk.search",
	"aioslsk.server",
	"aioslsk.transfer",
):
	logging.getLogger(name).setLevel(logging.CRITICAL)

QUERY = "Ja Rule - Clap Back"
USERNAME = "DjLic"
PASSWORD = "DjLic"

ALLOWED_EXTS = {"flac", "mp3", "wav"}

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")


def _normalize(text: str) -> str:
	text = text.lower()
	# remove punctuation, dashes, underscores, spaces
	return re.sub(r"[^a-z0-9]+", "", text)


def _basename_without_ext(path: str) -> str:
	base = os.path.basename(path)
	# remove extension if present
	if "." in base:
		return base.rsplit(".", 1)[0]
	return base


def _similarity(a: str, b: str) -> float:
	return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _codec_priority(ext: str) -> int:
	ext = ext.lower().lstrip(".")
	# Higher is better; restrict to allowed
	if ext == "flac":
		return 105
	if ext == "wav":
		return 101
	if ext == "mp3":
		return 80
	return 0


def _infer_mp3_bitrate_from_name(name: str) -> int:
	n = name.lower()
	# Common hints in filenames
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


def _quality_tuple(filename: str, size: int, ext: str, sim: float):
	# Higher is better for all components
	codec = _codec_priority(ext)
	if ext == "mp3":
		bitrate_hint = _infer_mp3_bitrate_from_name(filename)
		return (codec, bitrate_hint, size, sim)
	# For lossless: prefer larger files (albums/mastering differences)
	return (codec, 0, size, sim)


async def _download_with_timeout(
	client: SoulSeekClient,
	username: str,
	remote_path: str,
	start_timeout: float = 5.0,
) -> bool:
	"""Start downloading remote_path from username.
	Returns True if we observed progress start within timeout, else False.
	Prints progress 1..100% and saved path, then returns.
	"""
	progress_started = asyncio.Event()
	complete_or_removed = asyncio.Event()
	last_printed_percent: Optional[int] = None
	finished_printed = False

	# Listeners
	async def on_added(event: TransferAddedEvent):
		pass

	async def _finish_if_needed(transfer, curr_bytes: int):
		nonlocal last_printed_percent, finished_printed
		fs = transfer.filesize or 0
		if fs > 0 and curr_bytes >= fs and (last_printed_percent or 0) < 100:
			print("download progress: 100%")
			last_printed_percent = 100
		if not finished_printed:
			path = transfer.local_path or "(unknown path)"
			print(f"download finished: {path}")
			finished_printed = True
		complete_or_removed.set()

	async def on_removed(event: TransferRemovedEvent):
		if event.transfer.username == username and event.transfer.remote_path == remote_path:
			await _finish_if_needed(event.transfer, event.transfer.bytes_transfered)

	async def on_progress(event: TransferProgressEvent):
		nonlocal last_printed_percent
		for transfer, prev, curr in event.updates:
			if transfer.username != username or transfer.remote_path != remote_path:
				continue
			filesize = transfer.filesize or 0
			# Mark started once bytes are moving or state indicates download
			if (curr.bytes_transfered > 0 or curr.state.VALUE == TransferState.State.DOWNLOADING) and not progress_started.is_set():
				print("download started")
				progress_started.set()
			# Compute percent if filesize known
			if filesize > 0 and curr.bytes_transfered >= 0:
				percent = int((curr.bytes_transfered / filesize) * 100)
				percent = max(1, min(100, percent))
				if last_printed_percent is None or percent > last_printed_percent:
					last_printed_percent = percent
					print(f"download progress: {percent}%")
				# If we have reached 100%, finalize immediately
				if percent >= 100:
					await _finish_if_needed(transfer, curr.bytes_transfered)
			# Detect terminal states and finish
			if curr.state.VALUE in (
				TransferState.State.COMPLETE,
				TransferState.State.INCOMPLETE,
				TransferState.State.ABORTED,
				TransferState.State.FAILED,
			):
				if not progress_started.is_set():
					progress_started.set()
				await _finish_if_needed(transfer, curr.bytes_transfered)

	client.events.register(TransferAddedEvent, on_added)
	client.events.register(TransferProgressEvent, on_progress)
	client.events.register(TransferRemovedEvent, on_removed)

	try:
		print(f"starting download: user={username} | path={remote_path}")
		transfer = await client.transfers.download(username, remote_path)

		# Wait for progress to begin
		try:
			await asyncio.wait_for(progress_started.wait(), timeout=start_timeout)
		except asyncio.TimeoutError:
			print("start timeout (5s) — aborting and trying next")
			try:
				await client.transfers.abort(transfer)
			except Exception:
				pass
			return False

		# Progress started; wait until completion/removed
		await complete_or_removed.wait()
		return True
	finally:
		# Unregister listeners to avoid accumulation between attempts
		client.events.unregister(TransferAddedEvent, on_added)
		client.events.unregister(TransferProgressEvent, on_progress)
		client.events.unregister(TransferRemovedEvent, on_removed)


async def main() -> None:
	# Ensure download directory exists
	os.makedirs(DOWNLOAD_DIR, exist_ok=True)

	settings = Settings(
		credentials=CredentialsSettings(username=USERNAME, password=PASSWORD)
	)
	# Point downloads to our project downloads directory
	settings.shares.download = DOWNLOAD_DIR
	# Ensure the search request times out promptly so the script ends
	settings.searches.send.request_timeout = 10  # seconds

	# Collect results until the search request is removed (timeout/complete)
	collected: list[tuple[str, str, int, str]] = []  # (user, filename, size, ext)
	stop_event = asyncio.Event()

	async with SoulSeekClient(settings=settings) as client:
		# Explicit login after start/connect
		await client.login()

		print(f"searching: '{QUERY}' (timeout {settings.searches.send.request_timeout}s)...")

		# Register listeners for search events
		async def on_result(event: SearchResultEvent):
			# Include both shared and locked results
			def iter_files() -> Iterable:
				for file in event.result.shared_items:
					yield file
				for file in event.result.locked_results:
					yield file

			for file in iter_files():
				ext = (file.extension or os.path.splitext(file.filename)[1][1:]).lower()
				if ext not in ALLOWED_EXTS:
					continue
				collected.append((
					event.result.username,
					file.filename,
					int(file.filesize),
					ext,
				))

		async def on_removed(event: SearchRequestRemovedEvent):
			# Fired when request times out or is removed. Stop waiting.
			stop_event.set()

		client.events.register(SearchResultEvent, on_result)
		client.events.register(SearchRequestRemovedEvent, on_removed)

		# Issue search request
		_ = await client.searches.search(QUERY)

		# Wait until timeout/removal
		await stop_event.wait()
		client.events.unregister(SearchResultEvent, on_result)
		client.events.unregister(SearchRequestRemovedEvent, on_removed)

		# If nothing collected, just exit quietly
		if not collected:
			print("no results")
			return

		print(f"search complete — {len(collected)} audio candidates (pre-filter)")

		# Compute name similarity to the query (ignoring punctuation etc.)
		target = _basename_without_ext(QUERY)
		with_scores = [(
			username,
			filename,
			size,
			ext,
			_similarity(_basename_without_ext(filename), target),
		) for (username, filename, size, ext) in collected]

		max_sim = max(x[4] for x in with_scores)
		# Keep near-best matches: within 0.05 of max similarity
		filtered = [x for x in with_scores if x[4] >= max_sim - 0.05]

		# Sort by quality (codec, bitrate hint for mp3, then size), then by similarity
		filtered.sort(key=lambda x: _quality_tuple(x[1], x[2], x[3], x[4]))
		filtered = list(reversed(filtered))

		print(f"trying {len(filtered)} best-matching results by quality...")

		# Try to download best to worst with 5s start timeout
		for idx, (username, filename, size, ext, sim) in enumerate(filtered, 1):
			print(f"candidate #{idx}: user={username} | ext={ext} | size={size} | sim={sim:.2f}")
			ok = await _download_with_timeout(client, username, filename, start_timeout=5.0)
			if ok:
				break
			else:
				print("moving to next candidate...")


if __name__ == "__main__":
	asyncio.run(main())
