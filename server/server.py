import asyncio
import json
import logging
import os
import time
from threading import Thread
from typing import Dict, Any

from flask import Flask, request, Response, stream_with_context
from flask_socketio import SocketIO, emit

from slsk_service import SoulseekService, DownloadEvent

# Configure logging to suppress verbose aioslsk peer connection errors
logging.getLogger('aioslsk').setLevel(logging.WARNING)
logging.getLogger('aioslsk.network').setLevel(logging.WARNING)
logging.getLogger('aioslsk.transfer').setLevel(logging.WARNING)

app = Flask(__name__)
# Let Flask-SocketIO auto-detect the best async mode
# It will prefer eventlet, then gevent, then threading
# For production with gunicorn, we can use eventlet or gevent
# If SOCKETIO_ASYNC_MODE is not set, Flask-SocketIO will auto-detect
# For development, use threading to avoid output buffering issues with eventlet
async_mode = os.environ.get("SOCKETIO_ASYNC_MODE", "threading")  # Default to threading for development
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=async_mode)

# Configure credentials and paths
USERNAME = "DjLic"
PASSWORD = "DjLic"
BASE_DIR = os.path.dirname(__file__)
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Run an event loop in a background thread for aioslsk
_loop = asyncio.new_event_loop()

def _handle_exception(loop, context):
	"""Handle unhandled exceptions in the event loop, especially from aioslsk background tasks."""
	exception = context.get('exception')
	message = context.get('message', '')
	
	# Filter out expected peer connection errors that are normal during downloads
	if exception:
		exception_type = type(exception).__name__
		exception_str = str(exception)
		
		# These are expected network errors during peer connection attempts
		if 'PeerConnectionError' in exception_type or 'PeerConnectionError' in exception_str:
			# Only log at debug level, these are expected
			if 'indirect connection' in exception_str or 'failed connect' in exception_str:
				# These are normal - peer might be behind NAT/firewall
				return
		
		# Log other unexpected exceptions
		print(f"[EVENT_LOOP] Unhandled exception: {exception_type}: {exception_str}")
		if 'Task exception was never retrieved' not in message:
			print(f"[EVENT_LOOP] Context: {message}")
	else:
		# Log non-exception context messages
		if 'Task exception was never retrieved' in message:
			# This is usually from aioslsk background tasks, can be ignored
			return
		print(f"[EVENT_LOOP] {message}")

def _run_loop(loop):
	asyncio.set_event_loop(loop)
	# Set exception handler to catch unhandled exceptions from background tasks
	loop.set_exception_handler(_handle_exception)
	loop.run_forever()

_thread = Thread(target=_run_loop, args=(_loop,), daemon=True)
_thread.start()

# Active jobs registry
_jobs: Dict[str, Dict[str, Any]] = {}
# Map job_id to client sids
_job_sids: Dict[str, str] = {}

@app.post("/download")
def http_start_download():
	print(f"[HTTP] POST /download received", flush=True)
	data = request.get_json(force=True) if request.data else {}
	query = (data.get("query") or "").strip()
	print(f"[HTTP] Query: {query}", flush=True)
	if not query:
		return {"error": "missing query"}, 400

	job_id = os.urandom(6).hex()
	print(f"[HTTP] Created job_id: {job_id}", flush=True)
	queue: asyncio.Queue = asyncio.Queue(maxsize=100)
	http_polling_events = []  # Separate list for HTTP polling clients
	confirmation_event = asyncio.Event()
	_jobs[job_id] = {
		"query": query,
		"queue": queue,
		"http_polling_events": http_polling_events,
		"path": None,
		"finished": False,
		"confirmation": confirmation_event,
		"confirmed": False,
	}

	preferred_format = data.get("format", "").strip().lower()  # "mp3" or "flac"
	if preferred_format and preferred_format not in ("mp3", "flac"):
		preferred_format = None
	
	svc = SoulseekService(USERNAME, PASSWORD, DOWNLOAD_DIR, search_timeout=5)

	async def run_job():
		format_msg = f" (preferred: {preferred_format})" if preferred_format else ""
		print(f"[JOB {job_id}] Starting download for: {query}{format_msg}", flush=True)
		try:
			async for ev in svc.download(query, preferred_format=preferred_format if preferred_format else None, confirmation_event=confirmation_event):
				print(f"[JOB {job_id}] Event received: {ev.kind} - {ev.message}", flush=True)
				# Track path/finished for streaming
				if ev.kind == "started" and ev.path:
					_jobs[job_id]["path"] = ev.path
				elif ev.kind == "finished":
					_jobs[job_id]["finished"] = True
				# Enqueue for WS consumers and HTTP polling
				try:
					event_dict = ev.__dict__
					await queue.put(event_dict)
					# Also add to HTTP polling events list (thread-safe append)
					_jobs[job_id]["http_polling_events"].append(event_dict)
					print(f"[JOB {job_id}] Event enqueued: {ev.kind}")
				except asyncio.CancelledError:
					print(f"[JOB {job_id}] Cancelled")
					break
			print(f"[JOB {job_id}] Download job completed")
		except Exception as e:
			print(f"[JOB {job_id}] ERROR in run_job: {e}")
			import traceback
			traceback.print_exc()
			await queue.put({"kind": "error", "message": str(e)})

	task = asyncio.run_coroutine_threadsafe(run_job(), _loop)
	_jobs[job_id]["task"] = task
	print(f"[JOB {job_id}] Task submitted to event loop", flush=True)
	# Check if task started successfully
	try:
		task.result(timeout=0.1)
	except asyncio.TimeoutError:
		pass  # Expected - task is running
	except Exception as e:
		print(f"[JOB {job_id}] Task error: {e}")
	return {"job_id": job_id}

@app.get("/poll/<job_id>")
def http_poll_job(job_id: str):
	"""HTTP polling endpoint for clients that don't support WebSocket"""
	print(f"[HTTP POLL] GET /poll/{job_id} received", flush=True)
	print(f"[HTTP POLL] Available jobs: {list(_jobs.keys())}", flush=True)
	job = _jobs.get(job_id)
	if not job:
		print(f"[HTTP POLL] Job {job_id} not found", flush=True)
		return {"error": "unknown job_id"}, 404
	
	http_polling_events = job.get("http_polling_events", [])
	
	# Get the next event from HTTP polling events list (FIFO)
	if http_polling_events:
		event = http_polling_events.pop(0)  # Remove and return first event
		event_kind = event.get('kind') if isinstance(event, dict) else 'unknown'
		print(f"[HTTP POLL] Returning event: {event_kind} (remaining: {len(http_polling_events)})", flush=True)
		return event, 200
	else:
		# No events available yet
		print(f"[HTTP POLL] No events available, returning waiting status", flush=True)
		return {"status": "waiting"}, 200

@app.post("/confirm/<job_id>")
def http_confirm_download(job_id: str):
	"""HTTP endpoint for confirming download (for clients without WebSocket)"""
	print(f"[HTTP CONFIRM] POST /confirm/{job_id} received", flush=True)
	print(f"[HTTP CONFIRM] Available jobs: {list(_jobs.keys())}", flush=True)
	job = _jobs.get(job_id)
	if not job:
		print(f"[HTTP CONFIRM] Job {job_id} not found", flush=True)
		return {"error": "unknown job_id"}, 404
	
	confirmation_event = job.get("confirmation")
	if confirmation_event:
		job["confirmed"] = True
		# Signal the confirmation event in the event loop
		asyncio.run_coroutine_threadsafe(confirmation_event.set(), _loop)
		print(f"[HTTP CONFIRM] Download confirmed for job: {job_id}", flush=True)
		return {"status": "confirmed"}, 200
	else:
		print(f"[HTTP CONFIRM] No confirmation event for job: {job_id}", flush=True)
		return {"error": "no confirmation event for this job"}, 400

@socketio.on("connect")
def on_connect():
	print(f"[WS] New WebSocket connection", flush=True)

@socketio.on("subscribe")
def on_subscribe(data):
	job_id = data.get("job_id")
	sid = request.sid
	print(f"[WS] Subscribe request for job: {job_id} from sid: {sid}", flush=True)
	if not job_id or job_id not in _jobs:
		print(f"[WS] ERROR: Job {job_id} not found in _jobs", flush=True)
		emit("error", {"kind": "error", "message": "unknown job_id"})
		return
	
	_job_sids[job_id] = sid
	print(f"[WS] Connected to job: {job_id}", flush=True)
	queue: asyncio.Queue = _jobs[job_id]["queue"]
	
	# Start background task to drain queue
	def send_events():
		print(f"[WS] Background task started for job {job_id}", flush=True)
		job_finished = False
		while not job_finished:
			try:
				print(f"[WS] Waiting for queue item...", flush=True)
				item = asyncio.run_coroutine_threadsafe(queue.get(), _loop).result(timeout=60)
				print(f"[WS] Got item from queue: {item.get('kind')}", flush=True)
				socketio.emit("progress", item, room=sid)
				print(f"[WS] Sent event: {item.get('kind')}", flush=True)
				if item.get("kind") == "finished" or item.get("kind") == "error":
					print(f"[WS] Job finished/error, stopping background task")
					job_finished = True
					break
			except TimeoutError:
				# Timeout is normal - just check if job is still active
				if job_id not in _jobs or _jobs[job_id].get("finished", False):
					print(f"[WS] Job finished, stopping background task")
					job_finished = True
					break
				# Continue waiting for more events
				continue
			except Exception as e:
				print(f"[WS] Error in loop: {e}")
				import traceback
				traceback.print_exc()
				# Only break on unexpected errors, not timeouts
				if "TimeoutError" not in str(type(e).__name__):
					break
	
	socketio.start_background_task(send_events)

@socketio.on("confirm_download")
def on_confirm_download(data):
	job_id = data.get("job_id")
	sid = request.sid
	print(f"[WS] Confirm download request received for job: {job_id} from sid: {sid}")
	print(f"[WS] Data received: {data}")
	if not job_id or job_id not in _jobs:
		print(f"[WS] ERROR: Unknown job_id: {job_id}")
		emit("error", {"kind": "error", "message": "unknown job_id"})
		return
	
	job = _jobs[job_id]
	confirmation_event = job.get("confirmation")
	if confirmation_event:
		job["confirmed"] = True
		# Signal the confirmation event in the event loop
		print(f"[WS] Setting confirmation event for job: {job_id}")
		asyncio.run_coroutine_threadsafe(confirmation_event.set(), _loop)
		print(f"[WS] Download confirmed for job: {job_id}")
		emit("progress", {"kind": "status", "message": "Download confirmed, starting..."}, room=sid)
	else:
		print(f"[WS] ERROR: No confirmation event for job: {job_id}")
		emit("error", {"kind": "error", "message": "no confirmation event for this job"})

@app.get("/stream/<job_id>")
def http_stream_file(job_id: str):
	job = _jobs.get(job_id)
	if not job:
		return {"error": "unknown job_id"}, 404

	def generate():
		current_path = None
		pos = 0
		last_growth = time.time()
		file_to_delete = None
		try:
			while True:
				# Switch to latest path if changed
				latest = job.get("path")
				if latest and latest != current_path:
					current_path = latest
					pos = 0
					# Track the file path for deletion
					file_to_delete = current_path if os.path.isabs(current_path) else os.path.join(DOWNLOAD_DIR, os.path.basename(current_path))
				# If we don't yet have a path and not finished, wait
				if not current_path and not job.get("finished"):
					time.sleep(0.1)
					continue
				if not current_path and job.get("finished"):
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
					# If switched to a new path or not yet created, wait
					if job.get("finished") and job.get("path") == current_path:
						break
					time.sleep(0.2)
		finally:
			# Delete the file after streaming is complete
			if file_to_delete and os.path.exists(file_to_delete):
				try:
					os.remove(file_to_delete)
					print(f"[STREAM] Deleted file after streaming: {file_to_delete}")
				except Exception as e:
					print(f"[STREAM] Error deleting file {file_to_delete}: {e}")

	return Response(stream_with_context(generate()), mimetype="application/octet-stream")

if __name__ == "__main__":
	# Development mode - use Flask dev server
	import sys
	sys.stdout.flush()  # Ensure output is not buffered
	port = int(os.environ.get("PORT", 8001))
	print(f"[SERVER] Starting server on port {port}...", flush=True)
	print(f"[SERVER] SocketIO async_mode: {socketio.async_mode}", flush=True)
	print(f"[SERVER] Download directory: {DOWNLOAD_DIR}", flush=True)
	print(f"[SERVER] Server will be available at http://0.0.0.0:{port}", flush=True)
	print(f"[SERVER] Press CTRL+C to stop", flush=True)
	try:
		socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
	except KeyboardInterrupt:
		print("\n[SERVER] Shutting down...", flush=True)
	except Exception as e:
		print(f"[SERVER] Error starting server: {e}", flush=True)
		import traceback
		traceback.print_exc()
else:
	# Production mode - gunicorn will import this module
	# The app and socketio objects are already created above
	pass

