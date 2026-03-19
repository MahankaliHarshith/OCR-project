"""
Start the Receipt Scanner with a Dev Tunnel for public access.

Usage:
    python start_devtunnel.py
"""

import contextlib
import os
import signal
import subprocess
import sys
import time
import urllib.request


def main():
    port = int(os.getenv("API_PORT", "8000"))
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    print("=" * 60)
    print("  📝 Handwritten Receipt Scanner — DEV TUNNEL MODE")
    print("=" * 60)

    # ── Step 1: Start Uvicorn server in background subprocess ──
    print(f"\n  🔄 Starting server on port {port}...")

    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    server_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "0.0.0.0", "--port", str(port),
         "--timeout-keep-alive", "120"],
        cwd=project_dir,
        creationflags=creation_flags,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        encoding="utf-8",
        errors="replace",
    )

    # Read server output in a thread so it doesn't block
    import threading

    def stream_server_output():
        for line in server_process.stdout:
            line = line.rstrip()
            if line:
                print(f"  [server] {line}")

    server_thread = threading.Thread(target=stream_server_output, daemon=True)
    server_thread.start()

    # Wait for server to be ready (up to 60s for OCR model loading)
    for i in range(60):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/dashboard")
            resp = urllib.request.urlopen(req, timeout=3)
            if resp.status == 200:
                print(f"\n  ✅ Server is running on http://localhost:{port}")
                break
        except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError):
            if i % 5 == 0 and i > 0:
                print(f"  ⏳ Still waiting for server... ({i}s)")
            time.sleep(1)
    else:
        if server_process.poll() is not None:
            print("  ❌ Server failed to start. Check errors above.")
            sys.exit(1)
        print("  ⚠  Server didn't respond in 60s. It might still be loading...")

    # ── Step 2: Create Dev Tunnel ──
    print("\n  🌐 Creating Dev Tunnel...")

    try:
        tunnel_process = subprocess.Popen(
            ["devtunnel", "host", "-p", str(port), "--allow-anonymous"],
            cwd=project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creation_flags,
        )

        # Read tunnel output to find the URL
        tunnel_url = None
        start_time = time.time()
        while time.time() - start_time < 30:  # Wait up to 30s for tunnel URL
            line = tunnel_process.stdout.readline()
            if not line:
                if tunnel_process.poll() is not None:
                    print("  ❌ Tunnel process exited unexpectedly.")
                    break
                time.sleep(0.5)
                continue
            line = line.strip()
            if line:
                print(f"  [tunnel] {line}")
            if "https://" in line and "devtunnels" in line:
                # Extract URLs — prefer the short browseable URL, skip -inspect
                for word in line.replace(",", " ").split():
                    if word.startswith("https://") and "devtunnels" in word and "-inspect" not in word and (tunnel_url is None or "-8000." in word):
                        # Prefer the -8000. URL over the :8000 URL
                        tunnel_url = word.rstrip(",")
            if "Ready to accept" in line:
                break

        # Stream remaining tunnel output in a thread
        def stream_tunnel_output():
            for tl in tunnel_process.stdout:
                tl = tl.rstrip()
                if tl:
                    print(f"  [tunnel] {tl}")

        tunnel_thread = threading.Thread(target=stream_tunnel_output, daemon=True)
        tunnel_thread.start()

        if tunnel_url:
            print("\n" + "=" * 60)
            print("  🎉 DEV TUNNEL READY!")
            print("=" * 60)
            print(f"\n  🌍 Public URL  : {tunnel_url}")
            print(f"  🏠 Local URL   : http://localhost:{port}")
            print(f"  📄 API Docs    : {tunnel_url}/docs")
            print("\n  📱 Open this URL on your phone or other device!")
            print("  ⏱  Tunnel stays active while this script runs.")
            print("=" * 60)
        else:
            print("  ⚠  Couldn't detect tunnel URL. Check the output above.")

        print("\n  Press Ctrl+C to stop.\n")

    except FileNotFoundError:
        print("  ❌ devtunnel CLI not found. Install it:")
        print("     winget install Microsoft.devtunnel")
        server_process.terminate()
        sys.exit(1)
    except Exception as e:
        print(f"  ❌ Tunnel creation failed: {e}")
        server_process.terminate()
        sys.exit(1)

    # ── Step 3: Keep running until Ctrl+C ──
    def cleanup(signum=None, frame=None):
        print("\n\n  🛑 Shutting down...")
        try:
            tunnel_process.terminate()
            tunnel_process.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                tunnel_process.kill()
        try:
            server_process.terminate()
            server_process.wait(timeout=5)
        except Exception:
            with contextlib.suppress(Exception):
                server_process.kill()
        print("  ✅ Server and tunnel stopped.\n")
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        # Keep alive by monitoring both processes and printing heartbeat
        heartbeat = 0
        while True:
            if server_process.poll() is not None:
                print("  ⚠  Server process exited. Restarting...")
                server_process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.main:app",
                     "--host", "0.0.0.0", "--port", str(port),
                     "--timeout-keep-alive", "120"],
                    cwd=project_dir,
                    creationflags=creation_flags,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                    encoding="utf-8",
                    errors="replace",
                )
                st = threading.Thread(target=stream_server_output, daemon=True)
                st.start()
                time.sleep(10)
            if tunnel_process.poll() is not None:
                print("  ⚠  Tunnel process exited unexpectedly.")
                break
            heartbeat += 1
            if heartbeat % 30 == 0:
                # Print heartbeat every 60s to keep terminal alive
                print(f"  💓 Running... (uptime: {heartbeat * 2}s)")
            time.sleep(2)
    except KeyboardInterrupt:
        cleanup()

    cleanup()


if __name__ == "__main__":
    main()
