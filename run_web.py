"""Start the web dashboard: python run_web.py

Env vars: WEB_HOST (default 127.0.0.1), WEB_PORT (default 8000),
WEB_RELOAD=1 to enable auto-reload (dev only).
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "web.app:app",
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WEB_PORT", "8000")),
        reload=os.getenv("WEB_RELOAD") == "1",
        # Hard cap on graceful shutdown: in-flight scans stop at their next
        # checkpoint (≤ ~60s Playwright stretch); never hang the old process
        # past this or a restart hits "address already in use".
        timeout_graceful_shutdown=90,
    )
