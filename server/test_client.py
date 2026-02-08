#!/usr/bin/env python3
"""
Simple test client to search for a song on the Soulseek API server.
Uses SocketIO for real-time events.
"""
import asyncio
import sys
import socketio

EXTERNAL_API_SERVER_URL = "http://localhost:8001"

async def test_search(query: str, preferred_format: str = "mp3"):
    """Test searching for a song"""
    print(f"[CLIENT] 🔍 Testing search for: '{query}' (format: {preferred_format})")
    print(f"[CLIENT] 📡 Connecting to server: {EXTERNAL_API_SERVER_URL}")

    # Create SocketIO client
    sio = socketio.AsyncClient()
    job_id = None
    finished = asyncio.Event()

    @sio.event
    async def connect():
        print(f"[CLIENT] ✅ Connected to server")

    @sio.event
    async def disconnect():
        print(f"[CLIENT] 🔌 Disconnected from server")

    @sio.on("progress")
    async def on_progress(data):
        nonlocal job_id
        kind = data.get("kind", "")
        message = data.get("message", "")

        if kind == "status":
            print(f"[CLIENT] 📊 Status: {message}")
        elif kind == "files_list":
            files = data.get("files", [])
            print(f"[CLIENT] 📋 Found {len(files)} candidate files")
            for i, f in enumerate(files[:5], 1):  # Show first 5 only
                print(f"  [{i}] {f}")
            if len(files) > 5:
                print(f"  ... and {len(files) - 5} more")
            # Send confirmation to start download
            print(f"[CLIENT] ✅ Sending confirmation to start download...")
            await sio.emit("confirm_download", {"job_id": job_id})
        elif kind == "progress":
            percent = data.get("percent", 0)
            print(f"[CLIENT] 📥 Download progress: {percent}%")
        elif kind == "started":
            path = data.get("path", "")
            print(f"[CLIENT] 🚀 Download started: {path}")
        elif kind == "error":
            print(f"[CLIENT] ❌ Error: {message}")
            finished.set()
        elif kind == "finished":
            path = data.get("path", "")
            print(f"[CLIENT] ✅ Finished! File: {path}")
            finished.set()

    try:
        # Connect to server
        await sio.connect(EXTERNAL_API_SERVER_URL)

        # Start download job via HTTP
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{EXTERNAL_API_SERVER_URL}/download",
                json={"query": query, "format": preferred_format, "search_timeout": 10}
            ) as resp:
                if resp.status != 200:
                    print(f"[CLIENT] ❌ Failed to create job: {resp.status}")
                    return False
                data = await resp.json()
                job_id = data.get("job_id")
                print(f"[CLIENT] ✅ Job created: {job_id}")

        # Subscribe to job events
        await sio.emit("subscribe", {"job_id": job_id})
        print(f"[CLIENT] 📡 Subscribed to job events")

        # Wait for download to complete (max 5 minutes)
        try:
            await asyncio.wait_for(finished.wait(), timeout=300)
        except asyncio.TimeoutError:
            print(f"[CLIENT] ⏱️ Timeout waiting for download to complete")

        return True

    except Exception as e:
        print(f"[CLIENT] ❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        if sio.connected:
            await sio.disconnect()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_client.py \"<song query>\" [format]")
        print("Example: python test_client.py \"Ja rule - Clap back\" mp3")
        print("Example: python test_client.py \"Ja rule - Clap back\" flac")
        sys.exit(1)

    query = sys.argv[1]
    preferred_format = sys.argv[2] if len(sys.argv) > 2 else "mp3"
    result = asyncio.run(test_search(query, preferred_format))
    sys.exit(0 if result else 1)
