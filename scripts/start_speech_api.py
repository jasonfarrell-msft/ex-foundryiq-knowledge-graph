#!/usr/bin/env python3
"""Serve the deployment's Functions handlers on loopback without stored keys."""

import os
import sys
from pathlib import Path


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    if os.environ.get("WEBSITE_INSTANCE_ID") or os.environ.get("WEBSITE_HOSTNAME"):
        raise SystemExit("Local development launcher must not run in Azure.")
    import uvicorn

    os.environ["SPEECH_LOCAL_DEVELOPMENT"] = "1"
    sys.path.insert(0, str(root / "api"))
    uvicorn.run("local_server:app", host="127.0.0.1", port=7071,
                access_log=False, lifespan="off")
