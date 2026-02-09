import asyncio
import json
import os
import shutil
import time
from threading import Thread
from typing import Dict, Any

from flask import Flask, request, Response, stream_with_context
from flask_socketio import SocketIO, emit

from slsk_service import SoulseekService, DownloadEvent

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Configure credentials and paths
USERNAME = "DjLic"
PASSWORD = "DjLic"
BASE_DIR = os.path.dirname(__file__)
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Run an event loop in a background thread for aioslsk
_loop = asyncio.new_event_loop()

def _run_loop(loop):
	asyncio.set_event_loop(loop)
	loop.run_forever()

_thread = Thread(target=_run_loop, args=(_loop,), daemon=True)
_thread.start()

# Active jobs registry
_jobs: Dict[str, Dict[str, Any]] = {}
# Map job_id to client sids
_job_sids: Dict[str, str] = {}

@app.post("/download")
def http_start_download():
	data = request.get_json(force=True) if request.data else {}
	query = (data.get("query") or "").strip()
	if not query:
		return {"error": "missing query"}, 400

	job_id = os.urandom(6).hex()
	queue: asyncio.Queue = asyncio.Queue(maxsize=100)
	confirmation_event = asyncio.Event()
	_jobs[job_id] = {
		"query": query,
		"queue": queue,
		"path": None,
		"finished": False,
		"confirmation": confirmation_event,
		"confirmed": False,
	}

	preferred_format = data.get("format", "").strip().lower()  # "mp3" or "flac"
	if preferred_format and preferred_format not in ("mp3", "flac"):
		preferred_format = None

	# Get search_timeout from request, default to 10 seconds
	search_timeout = data.get("search_timeout", 10)
	if not isinstance(search_timeout, int) or search_timeout < 5:
		search_timeout = 10
	if search_timeout > 60:
		search_timeout = 60  # Cap at 60 seconds

	svc = SoulseekService(USERNAME, PASSWORD, DOWNLOAD_DIR, search_timeout=search_timeout, job_id=job_id)

	async def run_job():
		format_msg = f" (preferred: {preferred_format})" if preferred_format else ""
		print(f"[JOB {job_id}] Starting download for: {query}{format_msg}")
		try:
			async for ev in svc.download(query, preferred_format=preferred_format if preferred_format else None, confirmation_event=confirmation_event):
				# Check if job was stopped
				if _jobs.get(job_id, {}).get("stopped"):
					print(f"[JOB {job_id}] Stopped by user")
					await queue.put({"kind": "cancelled", "message": "Stopped by user"})
					break
				# Log events - progress on same line, others on new line
				if ev.kind == "progress":
					print(f"\r[JOB {job_id}] Downloading: {ev.percent}%", end="", flush=True)
				else:
					print(f"\n[JOB {job_id}] {ev.kind}: {ev.message}")
				# Track path/finished for streaming
				if ev.kind == "started" and ev.path:
					_jobs[job_id]["path"] = ev.path
				elif ev.kind == "finished":
					_jobs[job_id]["finished"] = True
					# Also update path from finished event if available
					if ev.path:
						_jobs[job_id]["path"] = ev.path
				# Enqueue for WS consumers
				try:
					await queue.put(ev.__dict__)
				except asyncio.CancelledError:
					print(f"[JOB {job_id}] Cancelled")
					await queue.put({"kind": "cancelled", "message": "Cancelled"})
					break
			print(f"[JOB {job_id}] Download job completed")
		except asyncio.CancelledError:
			print(f"[JOB {job_id}] Cancelled by user")
			try:
				await queue.put({"kind": "cancelled", "message": "Cancelled by user"})
			except:
				pass
		except Exception as e:
			print(f"[JOB {job_id}] ERROR in run_job: {e}")
			import traceback
			traceback.print_exc()
			await queue.put({"kind": "error", "message": str(e)})

	task = asyncio.run_coroutine_threadsafe(run_job(), _loop)
	_jobs[job_id]["task"] = task
	print(f"[JOB {job_id}] Task submitted to event loop")
	# Check if task started successfully
	try:
		task.result(timeout=0.1)
	except asyncio.TimeoutError:
		pass  # Expected - task is running
	except Exception as e:
		print(f"[JOB {job_id}] Task error: {e}")
	return {"job_id": job_id}

@socketio.on("connect")
def on_connect():
	print(f"[WS] New WebSocket connection")

@socketio.on("subscribe")
def on_subscribe(data):
	job_id = data.get("job_id")
	sid = request.sid
	print(f"[WS] Subscribe request for job: {job_id} from sid: {sid}")
	if not job_id or job_id not in _jobs:
		emit("error", {"kind": "error", "message": "unknown job_id"})
		return

	_job_sids[job_id] = sid
	print(f"[WS] Connected to job: {job_id}")
	queue: asyncio.Queue = _jobs[job_id]["queue"]

	# Start background task to drain queue
	def send_events():
		while True:
			try:
				item = asyncio.run_coroutine_threadsafe(queue.get(), _loop).result(timeout=60)
				socketio.emit("progress", item, room=sid)
				if item.get("kind") == "finished":
					print(f"[WS] Job {job_id} finished")
					break
				elif item.get("kind") == "error":
					print(f"[WS] Job {job_id} error: {item.get('message')}")
					break
			except Exception as e:
				print(f"[WS] Error in job {job_id}: {e}")
				break

	socketio.start_background_task(send_events)

@socketio.on("confirm_download")
def on_confirm_download(data):
	job_id = data.get("job_id")
	sid = request.sid
	print(f"[WS] Confirm download request for job: {job_id} from sid: {sid}")
	if not job_id or job_id not in _jobs:
		emit("error", {"kind": "error", "message": "unknown job_id"})
		return

	job = _jobs[job_id]
	confirmation_event = job.get("confirmation")
	if confirmation_event:
		job["confirmed"] = True
		# Signal the confirmation event in the event loop (set() is not a coroutine, use call_soon_threadsafe)
		_loop.call_soon_threadsafe(confirmation_event.set)
		print(f"[WS] Download confirmed for job: {job_id}")
		emit("progress", {"kind": "status", "message": "Download confirmed, starting..."}, room=sid)
	else:
		emit("error", {"kind": "error", "message": "no confirmation event for this job"})

@socketio.on("stop_download")
def on_stop_download(data):
	job_id = data.get("job_id")
	sid = request.sid
	print(f"[WS] Stop download request for job: {job_id} from sid: {sid}")
	if not job_id or job_id not in _jobs:
		emit("error", {"kind": "error", "message": "unknown job_id"})
		return

	job = _jobs[job_id]
	# Mark job as stopped
	job["stopped"] = True
	# Cancel the task if it exists
	task = job.get("task")
	if task and not task.done():
		task.cancel()
		print(f"[WS] Job {job_id} cancelled")
	# Send cancelled event to client
	emit("progress", {"kind": "cancelled", "message": "Download stopped by user"}, room=sid)
	print(f"[WS] Download stopped for job: {job_id}")

@app.get("/stream/<job_id>")
def http_stream_file(job_id: str):
	job = _jobs.get(job_id)
	if not job:
		return {"error": "unknown job_id"}, 404

	def cleanup_job_dir():
		"""Delete the job subdirectory after streaming is complete"""
		job_subdir = os.path.join(DOWNLOAD_DIR, job_id)
		if os.path.exists(job_subdir) and os.path.isdir(job_subdir):
			try:
				shutil.rmtree(job_subdir)
				print(f"[STREAM] Deleted job directory after streaming: {job_subdir}")
			except Exception as e:
				print(f"[STREAM] Error deleting job directory {job_subdir}: {e}")

	def generate():
		current_path = None
		pos = 0
		last_growth = time.time()
		print(f"[STREAM] Starting generate() for job {job_id}")
		print(f"[STREAM] Job state: path={job.get('path')}, finished={job.get('finished')}")
		try:
			while True:
				# Switch to latest path if changed
				latest = job.get("path")
				if latest and latest != current_path:
					print(f"[STREAM] Switching to path: {latest}")
					current_path = latest
					pos = 0
				# If we don't yet have a path and not finished, wait
				if not current_path and not job.get("finished"):
					time.sleep(0.1)
					continue
				if not current_path and job.get("finished"):
					print(f"[STREAM] No path and finished=True, breaking with 0 bytes")
					break

				abs_path = current_path if os.path.isabs(current_path) else os.path.join(DOWNLOAD_DIR, os.path.basename(current_path))
				try:
					with open(abs_path, "rb") as f:
						f.seek(pos)
						chunk = f.read(64 * 1024)
						if chunk:
							pos += len(chunk)
							last_growth = time.time()
							yield chunk
						else:
							# No new data
							if job.get("finished") and job.get("path") == current_path:
								break
							# If job switched to a new path, loop will reopen
							if job.get("path") != current_path:
								time.sleep(0.1)
								continue
							# If stalled for too long, wait but allow switch/finish
							if time.time() - last_growth > 30:
								time.sleep(0.5)
							else:
								time.sleep(0.2)
				except FileNotFoundError:
					print(f"[STREAM] FileNotFoundError for path: {abs_path}")
					# If switched to a new path or not yet created, wait
					if job.get("finished") and job.get("path") == current_path:
						print(f"[STREAM] File not found and job finished, breaking")
						break
					time.sleep(0.2)
		finally:
			print(f"[STREAM] Finished streaming, total bytes sent from pos: {pos}")
			cleanup_job_dir()

	response = Response(stream_with_context(generate()), mimetype="application/octet-stream")
	response.call_on_close(cleanup_job_dir)
	return response

if __name__ == "__main__":
	port = int(os.environ.get("PORT", 8001))
	socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
