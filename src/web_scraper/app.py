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

def get_connection() -> pyodbc.Connection:
    driver = os.getenv("AZURE_SQL_DRIVER", "ODBC Driver 18 for SQL Server")
    connection_string = (
        f"DRIVER={{{driver}}};"
        f"SERVER=tcp:{os.environ['AZURE_SQL_SERVER']},1433;"
        f"DATABASE={os.environ['AZURE_SQL_DATABASE']};"
        f"UID={os.environ['AZURE_SQL_USERNAME']};"
        f"PWD={os.environ['AZURE_SQL_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate-no;Connection Timeout=30;"
    )
    return pyodbc.connect(connection_string, autocommit=False)

def start_run(connection: pyodbc.Connection, run_id: str):
    connection.execute(
        """INSERT INTO dbo.IngestionRuns(RunId, StartedAt, SitemapUrl, Status) VALUES(?, SYSUTCDATETIME(), ?, 'Running')""",
        run_id, SITEMAP_URL
    )
    connection.commit()

def finish_run(connection: pyodbc.Connection, run_id, discovered, success, failure, status, error = None):
    connection.execute(
        """UPDATE dbo.IngestionRuns SET CompletedAt=SYSUTCDATETIME(), DiscoveredCount=?, SuccessCount=?,
            FailureCount=?, Status=?, ErrorMessage=?
            WHERE RunId=?
        """,
        discovered, success, failure, status, error, run_id
    )
    connection.commit()

def save_product(connection: pyodbc.Connection, item: dict[str, Any]):
    p = item["product"]
    cursor = connection.cursor()
    try:
        existing = cursor.execute(
            "SELECT ContentHash FROM dbo.Products WHERE ProductId=?", p["product_id"]
        ).fetchone()

        cursor.execute(
            """
                MERGE dbo.Products AS target
                USING (SELECT ? ProductId) AS source
                ON target.ProductId = source.ProductId
                WHEN MATCHED THEN UPDATED SET
                    Handle=?, Title=?, Vendor=?, ProductType=?, DecriptionHtml=?, DescriptionText=?,
                    Price=?, MinimumPrice=?, MaximumPrice=?, CompareAtPrice=?, CurrencyCode=?, IsAvailable=?, PriceVaries=?,
                    TagsJson=?, OptionsJson=?, FeaturedImageUrl=?, ProductUrl=?, ProductJsonUrl=?, ShopifyCreatedAt=?,
                    ShopifyPublishedAt=?, LastScraptedAt=?, LastSeenInSitemapAt=?, IsActive=?, RawProductJson=?, ContentHash=?
                WHEN NOT MATCHED THEN INSERT
                    (
                        ProductId, Handle, Vendor, ProductType, DescriptionHtml, DescriptionText, Price, MinimumPrice, MaximumPrice,
                        CompareAtPrice, CurrencyCOde, IsAvailable, PriceVaries, TagsJson, OptionsJson, FeatureImageUrl, ProductUrl,
                        ProductJsonUrl, ShopifyCreateAt, ShopifyPublishedAt, LastScraptedAt, LastSeenInSitemapAt, IsActive, RawProductJson, ContentHash
                    )
                VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?);
            """,
            p['product_id'],
            # UPDATE Values
            p['handle'], p['title'], p['vendor'], p['product_type'], p['description_html'], p['description_text'], p['price'],
            p['minimum_price'], p['maximum_price'], p['compare_at_price'], p['currency_code'], p['is_available'], p['price_varies'],
            p['tags_json'], p['options_json'], p['featured_image_url'], p['product_url'], p['product_json_url'], p['shopify_created_at'],
            p['shopify_published_at'], p['last_scraped_at'], p['last_seen_at'], json.dumps(item.get("options") or [], ensure_ascii=False)
            , p['content_hash'],
            # INSERT Values
            p['handle'], p['title'], p['vendor'], p['product_type'], p['description_html'], p['description_text'], p['price'],
            p['minimum_price'], p['maximum_price'], p['compare_at_price'], p['currency_code'], p['is_available'], p['price_varies'],
            p['tags_json'], p['options_json'], p['featured_image_url'], p['product_url'], p['product_json_url'], p['shopify_created_at'],
            p['shopify_published_at'], p['last_scraped_at'], p['last_seen_at'], json.dumps(item.get("options") or [], ensure_ascii=False)
            , p['content_hash']
        )

        # Refresh child rows only when content changed or product is new.
        if existing is None or existing[0] != p['content_hash']:
            cursor.execute("DELETE FROM dbo.ProductVariants WHERE ProductId=?", p["product_id"])
            cursor.execute("DELETE FROM dbo.ProductImages WHERE ProductId=?", p["product_id"])

            for v in item["variants"]:
                cursor.execute(
                    """
                        INSERT INTO dbo.ProductVariants
                        (VariantId, ProductId, VariantTitle, Sku, Option1, Option2, Option3, OptionJson, Price, CompareAtPrice,
                        IsAvailable, IsTaxable, RequireShipping, Weight, FeaturedImageUrl, LastUpdatedAt)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    v["variant_id"], p["product_id"], v["variant_title"], v["sku"], v["option_1"], v["option_2"], v["option_3"],
                    v["options_json"], v["price"], v["compare_at_price"], v["is_available"], v["is_taxable"], v["requires_shipping"],
                    v["weight"], v["featured_image_url"], v["last_updated_at"]
                )

            for image in item["images"]:
                cursor.execute(
                    "INSERT INTO dbo.ProductImages(ProductId, ImageUrl, DisplayOrder) VALUES(?, ?, ?)",
                    p["product_id"], image["image_url"], image["display_order"]
                )
        connection.commit()
    except:
        connection.rollback()
        raise()
    finally:
        cursor.close()

def mark_missing_inactive(connection: pyodbc.Connection, run_started_at: datetime):
    # Only call this after a complete sitemap run. It avoids deleting products.
    connection.execute("UPDATE dbo.Products SET IsActive = 0 WHERE LastSeenInSitemapAt < ?", run_started_at)
    connection.commit()

def main():
    run_id = str(uuid.uuid4())
    run_started_at = utc_now()
    success = failure = 0
    connection = get_connection()
    session = build_http_session()
    start_run(connection, run_id)

    try:
        product_urls = get_product_urls(session, SITEMAP_URL)
        if not product_urls:
            raise RuntimeError("The sitemap returned zero product URLs")

        for number, product_url in enumerate(product_urls, start=1):
            json_url = to_product_json_url(product_url)
            try:
                raw = fetch_product(session, json_url)
                item = normalise(raw, product_url, json_url)
                save_product(connection, item)
                success += 1
                logger.info("[%d/%d] Saved %s", number, len(product_urls), item["product"]["handle"])
            except Exception as ex:
                failure += 1
                logger.exception("[%d/%d] Failed %s: %s", number, len(product_urls), json_url, ex)
            time.sleep(DELAY_SECONDS)

        # Mark missing products inactive only if every product was processed successfully.
        if failure == 0:
            mark_missing_inactive(connection, run_started_at)
        
        status = "Completed" if failure == 0 else "CompletedWithErrors"
        finish_run(connection, run_id, len(product_urls), success, failure, status)
        logger.info("Finished. Success=%d Failure=%d", success, failure)

    except Exception as ex:
        finish_run(connection, run_id, 0, success, failure, "Failed", str(ex))
        raise
    finally:
        session.close()
        connection.close()

if __name__ == "__main__":
    main()