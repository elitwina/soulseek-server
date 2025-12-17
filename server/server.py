import asyncio
import json
import os
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
	_jobs[job_id] = {
		"query": query,
		"queue": queue,
		"path": None,
		"finished": False,
	}

	preferred_format = data.get("format", "").strip().lower()  # "mp3" or "flac"
	if preferred_format and preferred_format not in ("mp3", "flac"):
		preferred_format = None
	
	svc = SoulseekService(USERNAME, PASSWORD, DOWNLOAD_DIR, search_timeout=10)

	async def run_job():
		format_msg = f" (preferred: {preferred_format})" if preferred_format else ""
		print(f"[JOB {job_id}] Starting download for: {query}{format_msg}")
		try:
			async for ev in svc.download(query, preferred_format=preferred_format if preferred_format else None):
				print(f"[JOB {job_id}] Event received: {ev.kind} - {ev.message}")
				# Track path/finished for streaming
				if ev.kind == "started" and ev.path:
					_jobs[job_id]["path"] = ev.path
				elif ev.kind == "finished":
					_jobs[job_id]["finished"] = True
				# Enqueue for WS consumers
				try:
					await queue.put(ev.__dict__)
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
		print(f"[WS] Background task started for job {job_id}")
		while True:
			try:
				print(f"[WS] Waiting for queue item...")
				item = asyncio.run_coroutine_threadsafe(queue.get(), _loop).result(timeout=60)
				print(f"[WS] Got item from queue: {item.get('kind')}")
				socketio.emit("progress", item, room=sid)
				print(f"[WS] Sent event: {item.get('kind')}")
				if item.get("kind") == "finished":
					print(f"[WS] Job finished, stopping background task")
					break
			except Exception as e:
				print(f"[WS] Error in loop: {e}")
				import traceback
				traceback.print_exc()
				break
	
	socketio.start_background_task(send_events)

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
	port = int(os.environ.get("PORT", 8001))
	socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)

