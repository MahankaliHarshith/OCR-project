"""
Server startup script.
Run this file to start the Receipt Scanner application.

Usage:
    python run.py

Environment Variables:
    LOG_LEVEL  - Set console log verbosity: DEBUG | INFO | WARNING | ERROR
                 (default: DEBUG).  File always logs at DEBUG level.
                 Example:  LOG_LEVEL=WARNING python run.py
"""

import uvicorn
from app.config import API_HOST, API_PORT, API_DEBUG, LOG_FILE, LOG_LEVEL

if __name__ == "__main__":
    print("=" * 50)
    print("  📝 Handwritten Receipt Scanner v1.0.0")
    print("=" * 50)
    print(f"  🌐 App:      http://localhost:{API_PORT}")
    print(f"  📄 Docs:     http://localhost:{API_PORT}/docs")
    print(f"  📋 Log file: {LOG_FILE}")
    print(f"  📊 Level:    {LOG_LEVEL}")
    print("=" * 50)

    uvicorn.run(
        "app.main:app",
        host=API_HOST,
        port=API_PORT,
        reload=API_DEBUG,  # False by default; set API_DEBUG=true in .env for dev
        log_level="debug" if API_DEBUG else "info",
    )
