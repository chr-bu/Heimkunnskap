"""python -m heimkunnskap.web: the web app and Claude connector."""

import logging

import uvicorn

from ..config import settings
from .app import create_app

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
uvicorn.run(create_app(), host="0.0.0.0", port=settings.web_listen_port,
            proxy_headers=False, server_header=False, access_log=False)
