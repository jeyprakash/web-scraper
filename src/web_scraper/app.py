import hashlib
import html
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import pyodbc
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

SITEMAP_URL = os.environ["PRODUCT_SITEMAP_URL"]
STORE_BASE_URL = os.environ["STORE_BASE_URL"]
CURRENCY = os.environ["CURRENCY"]
DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "0.5"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
USER_AGENT = "JewelleryCataloguePOC/1.0 (authorised catalogue ingestion)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("catalogue-ingestion")

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def build_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total = MAX_RETRIES,
        connect = MAX_RETRIES,
        read = MAX_RETRIES,
        status = MAX_RETRIES,
        backoff_factor = 1.0,
        status_forcelist = (429, 500, 502, 503, 504),
        allowed_methods = frozenset({"GET"}),
        respect_retry_after_header = True,
    )
    session.mount("https://", HTTPAdapter(max_retries = retry))
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/xml, text/xml;q=0.9, */*;q=0.8",
    })
    return session

