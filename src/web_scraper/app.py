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

def get_product_urls(session: requests.Session, sitemap_url: str) -> list[str]:
    logger.info("Reading sitemap urls: %s", sitemap_url)
    response = session.get(sitemap_url, timeout=30)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls: list[str] = []

    for loc in root.findall(".//sm:url/sm:loc", namespace):
        if loc.text:
            url = loc.text.strip()
            if "/products/" in url:
                urls.append(url)

    # Remove duplicates while keeping sitemap order.
    unique_urls = list(dict.fromkeys(urls))
    logger.info("Discovered %d product URLs", len(unique_urls))
    return urls

def to_product_json_url(product_url: str) -> str:
    parts = urlsplit(product_url)
    path = parts.path.strip("/")
    if not path.endswith(".js"):
        path += ".js"

    # Product JSON should not inherit sitemap/product-page query fragments.
    return urlunsplit((parts.scheme, parts.netloc, path, ", "))

def fetch_product(session: requests.Session, json_url: str) -> dict[str, Any]:
    response = session.get(json_url, timeout=30)
    response.raise_for_status()

    data = response.json()
    for field in ("id", "handle", "title"):
        if not data.get(field):
            raise ValueError(f"Missing required field: {field}")
    return data


