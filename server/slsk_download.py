import asyncio
import os
from typing import AsyncIterator, Optional

from aioslsk.client import SoulSeekClient
from aioslsk.events import (
	TransferProgressEvent,
	TransferRemovedEvent,
)
from aioslsk.transfer.manager import TransferState

from slsk_models import DownloadEvent


async def download_candidates(
	client: SoulSeekClient,
	candidates: list[tuple[str, str, int, str, float]],
	fallback_candidates: list[tuple[str, str, int, str, float]],
	confirmation_event: Optional[asyncio.Event] = None,
	rejected_event: Optional[asyncio.Event] = None,
) -> AsyncIterator[DownloadEvent]:
	"""
	Download candidates in parallel (up to 5 at a time).
	As soon as one starts successfully, abort all others.
	"""
	if not candidates and not fallback_candidates:
		return
	
	# Wait for confirmation if needed
	if confirmation_event is not None:
		if rejected_event is not None:
			# Wait for either confirmation or rejection
			done, pending = await asyncio.wait(
				[asyncio.create_task(confirmation_event.wait()), asyncio.create_task(rejected_event.wait())],
				return_when=asyncio.FIRST_COMPLETED
			)
			# Cancel the pending task
			for task in pending:
				task.cancel()
				try:
					await task
				except asyncio.CancelledError:
					pass
			
			if rejected_event.is_set():
				yield DownloadEvent(kind="error", message="Download rejected by client")
				return
		else:
			# Wait for confirmation only
			try:
				await asyncio.wait_for(confirmation_event.wait(), timeout=30.0)
				if not confirmation_event.is_set():
					yield DownloadEvent(kind="error", message="Confirmation timeout")
					return
			except asyncio.TimeoutError:
				yield DownloadEvent(kind="error", message="Confirmation timeout")
				return
	
	# Track users that have already failed
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
	
	# Process candidates in batches of 5
	batch_size = 5
	all_candidates = list(enumerate(all_candidates_to_try, 1))
	
	for batch_start in range(0, len(all_candidates), batch_size):
		batch = all_candidates[batch_start:batch_start + batch_size]
		batch_num = (batch_start // batch_size) + 1
		total_batches = (len(all_candidates) + batch_size - 1) // batch_size
		
		# Filter out already-failed users
		batch = [(idx, (username, filename, size, ext, sim)) for idx, (username, filename, size, ext, sim) in batch if username not in failed_users]
		
		if not batch:
			continue
		
		print(f"[DOWNLOAD] 📦 Batch {batch_num}/{total_batches}: Attempting {len(batch)} candidate(s) in parallel")
		yield DownloadEvent(kind="status", message=f"Batch {batch_num}/{total_batches}: Trying {len(batch)} candidate(s) in parallel")
		
		# Create download tasks for this batch
		download_tasks = []
		task_info = {}  # task -> (username, filename, queue, transfer, progress_evt, idx)
		
		for c_idx, (username, filename, size, ext, sim) in batch:
			print(f"[DOWNLOAD] 🔄 Candidate #{c_idx}: {username} | {ext} | {size} bytes")
			
			# Create queue for this candidate's events
			download_queue: asyncio.Queue[DownloadEvent] = asyncio.Queue(maxsize=1000)
			progress_started = asyncio.Event()
			finished_success = False
			started_sent = False
			last_percent: Optional[int] = None
			queued_notified = False
			
			async def download_single_candidate(
				c_username: str,
				c_filename: str,
				c_idx: int,
				c_queue: asyncio.Queue[DownloadEvent],
				c_progress_started: asyncio.Event,
				c_finished_success_ref: list[bool],
				c_started_sent_ref: list[bool],
				c_last_percent_ref: list[Optional[int]],
				c_queued_notified_ref: list[bool],
			):
				nonlocal failed_users
				
				async def _finish_if_needed(transfer, curr_bytes: int):
					nonlocal c_finished_success_ref, c_last_percent_ref
					fs = transfer.filesize or 0
					local_ok = False
					try:
						if transfer.local_path and os.path.exists(transfer.local_path):
							local_size = os.path.getsize(transfer.local_path)
							local_ok = fs > 0 and local_size >= fs
					except Exception:
						pass
					if fs > 0 and curr_bytes >= fs and (c_last_percent_ref[0] or 0) < 100:
						c_last_percent_ref[0] = 100
						await c_queue.put(DownloadEvent(kind="progress", percent=100, message="100%"))
						c_finished_success_ref[0] = True
					if (c_last_percent_ref[0] or 0) >= 100 or local_ok:
						c_finished_success_ref[0] = True
						await c_queue.put(DownloadEvent(kind="finished", path=transfer.local_path or ""))
				
				async def on_removed_t(event: TransferRemovedEvent):
					if event.transfer.username == c_username and event.transfer.remote_path == c_filename:
						await _finish_if_needed(event.transfer, event.transfer.bytes_transfered)
				
				async def on_progress_t(event: TransferProgressEvent):
					nonlocal c_started_sent_ref, c_last_percent_ref, c_queued_notified_ref
					for transfer, prev, curr in event.updates:
						if transfer.username != c_username or transfer.remote_path != c_filename:
							continue
						fs = transfer.filesize or 0
						
						# Notify queued state
						if curr.state == TransferState.State.QUEUED and not c_queued_notified_ref[0]:
							c_queued_notified_ref[0] = True
							await c_queue.put(DownloadEvent(kind="status", message=f"{c_username} queued"))
						
						# Check for downloading state
						state_type_name = type(curr.state).__name__
						state_str = str(curr.state)
						is_downloading_state = (
							curr.state == TransferState.State.DOWNLOADING or
							state_type_name == 'DownloadingState' or
							'Downloading' in state_str
						)
						is_complete_state = (
							curr.state == TransferState.State.COMPLETE or
							state_type_name == 'CompleteState' or
							'Complete' in state_str
						)
						
						# Mark as started if downloading or complete
						if (is_downloading_state or is_complete_state) and not c_progress_started.is_set():
							c_progress_started.set()
							if not c_started_sent_ref[0]:
								c_started_sent_ref[0] = True
								if is_complete_state:
									print(f"[DOWNLOAD] ✅ Candidate #{c_idx} ({c_username}) completed")
								else:
									print(f"[DOWNLOAD] ✅ Candidate #{c_idx} ({c_username}) started downloading")
								await c_queue.put(DownloadEvent(kind="started", path=transfer.local_path or ""))
						elif curr.bytes_transfered > 0 and curr.state != TransferState.State.QUEUED and not c_progress_started.is_set():
							c_progress_started.set()
							if not c_started_sent_ref[0]:
								c_started_sent_ref[0] = True
								print(f"[DOWNLOAD] ✅ Candidate #{c_idx} ({c_username}) started downloading")
								await c_queue.put(DownloadEvent(kind="started", path=transfer.local_path or ""))
						
						# Send progress events
						if fs > 0 and curr.bytes_transfered >= 0:
							percent = int((curr.bytes_transfered / fs) * 100)
							percent = max(1, min(100, percent))
							prev_p = 0 if c_last_percent_ref[0] is None else c_last_percent_ref[0]
							if percent > prev_p:
								# Send progress events immediately - don't batch
								for p in range(prev_p + 1, percent + 1):
									await c_queue.put(DownloadEvent(kind="progress", percent=p, message=f"{p}%"))
								c_last_percent_ref[0] = percent
								# Log progress for debugging - print every 10% or at 100%
								if percent % 10 == 0 or percent == 100:
									print(f"[DOWNLOAD] 📊 Progress: {percent}% ({curr.bytes_transfered}/{fs} bytes) from {c_username}")
						
						# Handle completion/failure
						if curr.state in (TransferState.State.COMPLETE, TransferState.State.INCOMPLETE, TransferState.State.ABORTED, TransferState.State.FAILED):
							finished_success_local = (curr.state == TransferState.State.COMPLETE) or (fs > 0 and curr.bytes_transfered >= fs)
							if finished_success_local:
								c_finished_success_ref[0] = True
							
							# Log if transfer failed/aborted before starting
							if curr.state == TransferState.State.FAILED and not c_progress_started.is_set():
								print(f"[DOWNLOAD] ❌ Candidate #{c_idx} ({c_username}): Transfer failed immediately (before starting)")
								await c_queue.put(DownloadEvent(kind="status", message=f"failed from {c_username}"))
								c_progress_started.set()
							elif curr.state == TransferState.State.ABORTED and not c_progress_started.is_set():
								print(f"[DOWNLOAD] ⚠️  Candidate #{c_idx} ({c_username}): Transfer aborted before starting")
								c_progress_started.set()
							
							# Set progress_started if transfer actually started or completed
							if curr.state in (TransferState.State.COMPLETE, TransferState.State.INCOMPLETE) or (curr.bytes_transfered > 0):
								c_progress_started.set()
							await _finish_if_needed(transfer, curr.bytes_transfered)
				
				client.events.register(TransferRemovedEvent, on_removed_t)
				client.events.register(TransferProgressEvent, on_progress_t)
				
				try:
					transfer_obj = await client.transfers.download(c_username, c_filename)
					
					# Immediately after starting download, check if transfer is already complete
					if transfer_obj:
						current_state = transfer_obj.state
						state_type_name = type(current_state).__name__
						is_complete = (
							current_state == TransferState.State.COMPLETE or
							state_type_name == 'CompleteState' or
							'Complete' in str(current_state)
						)
						if is_complete:
							# Transfer already complete - send progress events based on actual bytes
							fs = transfer_obj.filesize or 0
							bytes_transfered = transfer_obj.bytes_transfered or 0
							if fs > 0 and bytes_transfered >= fs:
								# Send started event
								await c_queue.put(DownloadEvent(kind="started", path=transfer_obj.local_path or ""))
								# Send 100% progress
								await c_queue.put(DownloadEvent(kind="progress", percent=100, message="100%"))
								# Send finished event
								await c_queue.put(DownloadEvent(kind="finished", path=transfer_obj.local_path or ""))
								print(f"[DOWNLOAD] 📊 Transfer already COMPLETE, sent progress events based on actual state")
					
					return c_queue, transfer_obj, c_progress_started, c_finished_success_ref[0]
				except Exception as download_error:
					error_str = str(download_error)
					error_type = type(download_error).__name__
					if ('PeerConnectionError' in error_type or 
						'peer' in error_str.lower() or 
						'connection' in error_str.lower() or
						'InvalidStateError' in error_type or
						'invalid state' in error_str.lower()):
						client.events.unregister(TransferRemovedEvent, on_removed_t)
						client.events.unregister(TransferProgressEvent, on_progress_t)
						failed_users.add(c_username)
						print(f"[DOWNLOAD] ❌ Candidate #{c_idx} ({c_username}): Connection failed")
						return None, None, None, False
					else:
						raise
				finally:
					# Clean up event handlers
					try:
						if 'on_removed_t' in locals():
							client.events.unregister(TransferRemovedEvent, on_removed_t)
						if 'on_progress_t' in locals():
							client.events.unregister(TransferProgressEvent, on_progress_t)
					except:
						pass
			
			# Create mutable refs for nested function
			finished_success_ref = [False]
			started_sent_ref = [False]
			last_percent_ref = [None]
			queued_notified_ref = [False]
			
			task = asyncio.create_task(
				download_single_candidate(
					username, filename, c_idx, download_queue, progress_started,
					finished_success_ref, started_sent_ref, last_percent_ref, queued_notified_ref
				)
			)
			download_tasks.append(task)
			task_info[task] = (username, filename, download_queue, None, progress_started, c_idx)
		
		# Wait a bit for transfers to initialize
		await asyncio.sleep(0.5)
		
		# Check which transfers have started
		found_successful = False
		successful_queue = None
		successful_transfer = None
		finished_success = False
		
		# Check all tasks to see if any are already complete
		for task in download_tasks:
			if task.done():
				try:
					queue, transfer, progress_evt, success = await task
					if success and queue:
						successful_queue = queue
						successful_transfer = transfer
						found_successful = True
						finished_success = success
						break
				except Exception as e:
					print(f"[DOWNLOAD] ⚠️  Task completed with error: {e}")
		
		# If no successful transfer found yet, wait for one to start
		if not found_successful:
			# Wait for at least one to start downloading (with timeout)
			progress_events = [task_info[task][4] for task in download_tasks if not task.done()]
			if progress_events:
				print(f"[DOWNLOAD] ⏳ Waiting for one of {len(progress_events)} candidate(s) to start downloading...")
				done, pending = await asyncio.wait(
					[asyncio.create_task(ev.wait()) for ev in progress_events],
					timeout=5.0,
					return_when=asyncio.FIRST_COMPLETED
				)
				
				# Find which transfer started
				for task in download_tasks:
					if not task.done():
						_, _, queue, transfer, progress_evt, _ = task_info[task]
						if progress_evt.is_set():
							# This one started - get the actual result
							try:
								queue, transfer, _, success = await task
								if queue:
									successful_queue = queue
									successful_transfer = transfer
									found_successful = True
									finished_success = success
									break
							except Exception as e:
								print(f"[DOWNLOAD] ⚠️  Error getting task result: {e}")
		
		# If we found a successful transfer, stream its events and abort others
		if found_successful and successful_queue:
			# Abort other transfers
			for task in download_tasks:
				if task != asyncio.current_task():
					try:
						if not task.done():
							_, _, _, transfer, _, _ = task_info[task]
							if transfer:
								try:
									current_state = transfer.state
									state_str = str(current_state)
									# Don't abort if already complete, downloading, failed, or aborted
									if (current_state != TransferState.State.COMPLETE and
										'Complete' not in state_str and
										current_state != TransferState.State.DOWNLOADING and
										'Downloading' not in state_str and
										current_state != TransferState.State.FAILED and
										'Failed' not in state_str and
										current_state != TransferState.State.ABORTED and
										'Aborted' not in state_str):
										await client.transfers.abort(transfer)
										print(f"[DOWNLOAD] 🛑 Aborted other candidate")
								except Exception as e:
									# Ignore abort errors
									pass
					except Exception as e:
						# Ignore errors
						pass
			
			# Stream events from successful download in real-time
			while not finished_success:
				try:
					ev = await asyncio.wait_for(successful_queue.get(), timeout=0.1)
					yield ev
					if ev.kind == "finished":
						finished_success = True
						print(f"[DOWNLOAD] ✅ Download completed successfully")
						break
					if ev.kind == "status" and "failed" in ev.message:
						print(f"[DOWNLOAD] ❌ Download failed: {ev.message}")
						failed_users.add(successful_transfer.username if successful_transfer else "unknown")
						finished_success = False
						break
				except asyncio.TimeoutError:
					# Check if transfer is complete
					if successful_transfer:
						try:
							current_state = successful_transfer.state
							is_complete = (
								current_state == TransferState.State.COMPLETE or
								type(current_state).__name__ == 'CompleteState' or
								'Complete' in str(current_state)
							)
							if is_complete:
								# Transfer complete - drain any remaining events
								while not successful_queue.empty():
									try:
										ev = await asyncio.wait_for(successful_queue.get(), timeout=0.1)
										yield ev
										if ev.kind == "finished":
											finished_success = True
											break
									except asyncio.TimeoutError:
										break
								
								# If no more events and file exists, we're done
								if successful_transfer.local_path and os.path.exists(successful_transfer.local_path):
									if not finished_success:
										yield DownloadEvent(kind="finished", path=successful_transfer.local_path)
										finished_success = True
										print(f"[DOWNLOAD] ✅ Download completed successfully (file exists: {successful_transfer.local_path})")
									break
						except Exception as e:
							# Error checking state - continue waiting
							pass
			
			if finished_success:
				return
		else:
			# No successful download in this batch
			print(f"[DOWNLOAD] ❌ Batch {batch_num}: No successful download, trying next batch")
			yield DownloadEvent(kind="status", message=f"Batch {batch_num}: All candidates failed or stuck in queue")
	
	# All batches failed
	if len(failed_users) >= len(unique_users_in_candidates):
		yield DownloadEvent(kind="error", message=f"All {len(unique_users_in_candidates)} unique user(s) failed. No more options available from search results.")



