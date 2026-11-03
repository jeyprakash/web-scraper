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


def money(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        # Shopify Ajax API returns money in the currency's minor unit.
        return (Decimal(str(value)) / Decimal("100")).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None

def plain_text(value: str | None) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(html.escape(value), "html.parser")
    result = " ".join(soup.get_text(" ", strip=True).split())
    return result or None

def absolute_url(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return STORE_BASE_URL + value
    return value

def parse_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalise(raw: dict[str, Any], product_url: str, json_url: str) -> dict[str, Any]:
    now = utc_now()
    raw_tags = raw.get("tags") or []
    tags = [x.strip() for x in raw_tags.split(",")] if isinstance(raw_tags, str) else raw_tags

    product = {
        "product_id": int(raw["id"]),
        "handle": str(raw["handle"]),
        "title": str(raw["title"]),
        "vendor": raw.get("vendor"),
        "product_type": raw.get("type"),
        "description_html": raw.get("description") or raw.get("body_html"),
        "description_text": plain_text(raw.get("description") or raw.get("body_html")),
        "price": money(raw.get("price")),
        "maximum_price": money(raw.get("price_max")),
        "minimum_price": money(raw.get("price_min")),
        "compare_at_price": money(raw.get("compare_at_price")),
        "currency_code": CURRENCY,
        "is_available": bool(raw.get("available", False)),
        "price_varies": bool(raw.get("price_varies", False)),
        "tags_json": json.dumps(tags, ensure_ascii=False),
        "options_json": json.dumps(raw.get("options") or [], ensure_ascii=False),
        "featured_image_url": absolute_url(raw.get("featured_image")),
        "product_url": product_url,
        "product_json_url": json_url,
        "shopify_created_at": parse_date(raw.get("created_at")),
        "shopify_published_at": parse_date(raw.get("published_at")),
        "last_scraped_at": now,
        "last_seen_at": now
    }

    variants = []
    for item in raw.get("variants") or []:
        image = item.get("featured_image")
        image_url = image.get("src") if isinstance(image, dict) else image
        variants.append({
            "variant_id": int(item["id"]),
            "variant_title": str(item.get("title")),
            "sku": item.get("sku"),
            "option1": item.get("option1"),
            "option2": item.get("option2"),
            "option3": item.get("option3"),
            "options_json": json.dumps(item.get("options") or [], ensure_ascii=False),
            "price": money(item.get("price")),
            "compare_at_price": money(item.get("compare_at_price")),
            "is_available": bool(item.get("available", False)),
            "is_taxable": bool(item.get("taxable", False)),
            "requires_shipping": item.get("requires_shipping"),
            "weight": item.get("weight"),
            "featured_image_url": absolute_url(image_url),
            "last_updated_at": now
        })

    images = []
    for position, item in enumerate(raw.get("iamges") or [], start=1):
        source = item.get("src") if isinstance(item, dict) else item
        url = absolute_url(source)

        if url:
            images.append({"image_url": url, "image_order": position})

    hash_payload = {
        "products": { k: str(v) for k, v in product.items() if k not in {"last_scraped_at", "last_seen_at"} },
        "vaiants": [{k: str(v) for k, v in x.items() if k != "last_updated_at"} for x in variants],
        "images": images
    }

    product["content_hash"] = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    return {"product": product, "vaiants": variants, "images": images, "raw": raw}