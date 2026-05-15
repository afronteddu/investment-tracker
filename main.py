#!/usr/bin/env python3
import os
from dotenv import load_dotenv

load_dotenv()

from src.api import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
