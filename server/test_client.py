#!/usr/bin/env python3
"""
Simple test client to download a song from the Soulseek API server
"""
import asyncio
import aiohttp
import json
import sys
import time

EXTERNAL_API_SERVER_URL = "http://localhost:8001"

async def test_download(query: str):
	"""Test downloading a song"""
	print(f"[CLIENT] 🔍 Testing download for: '{query}'")
	print(f"[CLIENT] 📡 Connecting to server: {EXTERNAL_API_SERVER_URL}")
	
	async with aiohttp.ClientSession() as session:
		# Start download job
		try:
			async with session.post(
				f"{EXTERNAL_API_SERVER_URL}/download",
				json={"query": query, "format": "mp3"},
				timeout=aiohttp.ClientTimeout(total=30)
			) as resp:
				if resp.status != 200:
					error_text = await resp.text()
					print(f"[CLIENT] ❌ Server error: {resp.status} - {error_text}")
					return False
				
				data = await resp.json()
				job_id = data.get("job_id")
				if not job_id:
					print(f"[CLIENT] ❌ Failed to get job_id from response: {data}")
					return False
				print(f"[CLIENT] ✅ Job created: {job_id}")
		except Exception as e:
			print(f"[CLIENT] ❌ Failed to create job: {e}")
			return False
		
		# Confirm download if needed
		print(f"[CLIENT] 📋 Confirming download...")
		try:
			async with session.post(
				f"{EXTERNAL_API_SERVER_URL}/confirm/{job_id}",
				timeout=aiohttp.ClientTimeout(total=10)
			) as confirm_resp:
				if confirm_resp.status == 200:
					print(f"[CLIENT] ✅ Download confirmed")
				else:
					print(f"[CLIENT] ⚠️  Confirm response: {confirm_resp.status}")
		except Exception as e:
			print(f"[CLIENT] ⚠️  Confirm error (may be OK): {e}")
		
		# Poll for progress
		print(f"[CLIENT] 📊 Polling for progress...")
		max_polls = 300  # 5 minutes max
		poll_count = 0
		last_status = ""
		
		while poll_count < max_polls:
			await asyncio.sleep(1)
			poll_count += 1
			
			try:
				async with session.get(
					f"{EXTERNAL_API_SERVER_URL}/poll/{job_id}",
					timeout=aiohttp.ClientTimeout(total=10)
				) as poll_resp:
					if poll_resp.status != 200:
						if poll_resp.status == 404:
							print(f"[CLIENT] ❌ Job not found (may have finished)")
							return False
						continue
					
					data = await poll_resp.json()
					
					# Check for error
					if data.get("error"):
						error = data.get("error")
						print(f"[CLIENT] ❌ Error: {error}")
						return False
					
					# Check status
					status = data.get("status", "")
					kind = data.get("kind", "")
					message = data.get("message", "")
					
					# Print status updates
					if status and status != last_status:
						print(f"[CLIENT] 📊 Status: {status}")
						last_status = status
					
					if kind:
						if kind == "finished":
							file_path = data.get("path", "")
							print(f"[CLIENT] ✅ Download completed successfully!")
							if file_path:
								print(f"[CLIENT] 📁 File: {file_path}")
							return True
						elif kind == "error":
							error_msg = message or data.get("error", "Unknown error")
							print(f"[CLIENT] ❌ Download failed: {error_msg}")
							return False
						elif kind == "started":
							file_path = data.get("path", "")
							if file_path:
								print(f"[CLIENT] 🚀 Download started: {file_path}")
						elif kind == "status" and message:
							if "waiting" not in message.lower() and "searching" not in message.lower():
								print(f"[CLIENT] ℹ️  {message}")
					
					# Check if job is finished (from status endpoint if available)
					if data.get("finished"):
						if data.get("success") or kind == "finished":
							file_path = data.get("file_path") or data.get("path", "")
							print(f"[CLIENT] ✅ Download completed successfully!")
							if file_path:
								print(f"[CLIENT] 📁 File: {file_path}")
							return True
						else:
							error = data.get("error", "Unknown error")
							print(f"[CLIENT] ❌ Download failed: {error}")
							return False
			except aiohttp.ClientError as e:
				print(f"[CLIENT] ⚠️  Network error: {e}")
				continue
			except Exception as e:
				print(f"[CLIENT] ⚠️  Poll error: {e}")
				continue
		
		print(f"[CLIENT] ⏱️  Timeout waiting for download")
		return False

if __name__ == "__main__":
	if len(sys.argv) < 2:
		print("Usage: python test_client.py <song_query>")
		sys.exit(1)
	
	query = sys.argv[1]
	result = asyncio.run(test_download(query))
	sys.exit(0 if result else 1)

