"""
Start the Receipt Scanner server in a subprocess isolated from VS Code's
terminal SIGINT.  Use VS Code's built-in Port Forwarding (Dev Tunnels)
to expose it to the internet.

Usage:
    python start_server.py
"""

import os, sys, subprocess, signal, time


def main():
    port = int(os.getenv("API_PORT", "8000"))

    print("=" * 55)
    print("  📝 Handwritten Receipt Scanner v1.0.0")
    print("=" * 55)
    print(f"  🏠 Local   : http://localhost:{port}")
    print(f"  📄 API Docs: http://localhost:{port}/docs")
    print(f"  ❤️  Health  : http://localhost:{port}/api/health")
    print("=" * 55)
    print("  💡 Use VS Code Ports panel to make this PUBLIC:")
    print("     1. Open PORTS panel (bottom bar, next to Terminal)")
    print('     2. Click "Forward a Port" → type 8000')
    print('     3. Change Visibility to "Public"')
    print("     4. Copy the public URL and share!")
    print("=" * 55)
    print()

    # Isolate subprocess from VS Code terminal SIGINT
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "0.0.0.0", "--port", str(port),
         "--log-level", "info"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        creationflags=flags,
    )

    def shutdown(signum=None, frame=None):
        print("\n  🛑 Shutting down server...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        print("  ✅ Done.\n")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        proc.wait()
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
