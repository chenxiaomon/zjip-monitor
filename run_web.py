"""Start the web dashboard: python run_web.py

Env vars: WEB_HOST (default 127.0.0.1), WEB_PORT (default 8000),
WEB_RELOAD=1 to enable auto-reload (dev only).
"""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "web.app:app",
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WEB_PORT", "8000")),
        reload=os.getenv("WEB_RELOAD") == "1",
    )
