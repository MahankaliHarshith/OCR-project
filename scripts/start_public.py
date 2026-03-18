"""
Start the Receipt Scanner with a PUBLIC internet-accessible URL.

This script:
    1. Starts the FastAPI/Uvicorn server on localhost:8000
    2. Creates an ngrok tunnel for public HTTPS access
    3. Prints the public URL for sharing / testing from any device

Usage:
    python start_public.py

    # With ngrok auth token (for longer sessions & custom domains):
    NGROK_AUTH_TOKEN=your_token_here python start_public.py

Requirements:
    pip install pyngrok

Notes:
    - Without an auth token, ngrok free tier gives ~2-hour sessions
    - With a free ngrok account (https://ngrok.com), sessions last longer
    - The public URL changes each restart unless you have a paid plan
"""

import os
import signal
import subprocess
import sys
import time


def main():
    port = int(os.getenv("API_PORT", "8000"))

    print("=" * 60)
    print("  📝 Handwritten Receipt Scanner — PUBLIC MODE")
    print("=" * 60)

    # ── Step 1: Start Uvicorn server in background ──
    print(f"\n  🔄 Starting server on port {port}...")

    # Use CREATE_NEW_PROCESS_GROUP on Windows to isolate from terminal SIGINT
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

    server_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "0.0.0.0", "--port", str(port),
         "--log-level", "info", "--no-access-log"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        creationflags=creation_flags,
    )

    # Wait for server to be ready
    import urllib.request
    for i in range(60):  # Wait up to 60 seconds for model loading
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
            resp = urllib.request.urlopen(req, timeout=3)
            if resp.status == 200:
                print(f"  ✅ Server is running on http://localhost:{port}")
                break
        except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError):
            if i % 5 == 0 and i > 0:
                print(f"  ⏳ Still waiting for server... ({i}s)")
            time.sleep(1)
    else:
        print("  ⚠  Server didn't respond in 60s, trying tunnel anyway...")

    # ── Step 2: Create ngrok tunnel ──
    print("\n  🌐 Creating public tunnel...")

    try:
        from pyngrok import ngrok

        # Set auth token if provided
        auth_token = os.getenv("NGROK_AUTH_TOKEN", "")
        if auth_token:
            ngrok.set_auth_token(auth_token)
            print("  🔑 Auth token configured")

        # Open tunnel
        tunnel = ngrok.connect(port, "http")
        public_url = tunnel.public_url

        # Force HTTPS
        if public_url.startswith("http://"):
            public_url = public_url.replace("http://", "https://", 1)

        print("\n" + "=" * 60)
        print("  🎉 PUBLIC URL READY!")
        print("=" * 60)
        print(f"\n  🌍 Public URL  : {public_url}")
        print(f"  🏠 Local URL   : http://localhost:{port}")
        print(f"  📄 API Docs    : {public_url}/docs")
        print(f"  ❤️  Health Check: {public_url}/api/health")
        print("\n  📱 Open on your phone or share with testers!")
        print("  ⏱  Session will stay active while this script runs.")
        print("=" * 60)
        print("\n  Press Ctrl+C to stop.\n")

    except ImportError:
        print("  ❌ pyngrok not installed. Run: pip install pyngrok")
        server_process.terminate()
        sys.exit(1)
    except Exception as e:
        print(f"  ❌ Tunnel creation failed: {e}")
        print(f"\n  💡 You can still access locally: http://localhost:{port}")
        print("  💡 To fix: sign up at https://ngrok.com and set NGROK_AUTH_TOKEN")
        print("\n  Press Ctrl+C to stop.\n")

    # ── Step 3: Keep running until Ctrl+C ──
    def cleanup(signum=None, frame=None):
        print("\n\n  🛑 Shutting down...")
        try:
            ngrok.disconnect(tunnel.public_url)
            ngrok.kill()
        except Exception:
            pass
        server_process.terminate()
        try:
            server_process.wait(timeout=5)
        except Exception:
            server_process.kill()
        print("  ✅ Server and tunnel stopped.\n")
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # Keep alive
    try:
        while server_process.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        cleanup()

    print("  ⚠  Server process exited unexpectedly.")
    cleanup()


if __name__ == "__main__":
    main()
