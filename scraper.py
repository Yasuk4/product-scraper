#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)

# ----------------------------
# Robots.txt (cached per host)
# ----------------------------
_ROBOTS_CACHE = {}

def get_robot_parser(base_url: str):
    """Fetch and cache robots.txt for a given base URL."""
    try:
        from urllib import robotparser
        parsed = urlparse(base_url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        if root in _ROBOTS_CACHE:
            return _ROBOTS_CACHE[root]
        rp = robotparser.RobotFileParser()
        rp.set_url(urljoin(root, "/robots.txt"))
        rp.read()
        _ROBOTS_CACHE[root] = rp
        return rp
    except Exception:
        return None

# ----------------------------
# Config dataclass
# ----------------------------
@dataclass
class SiteConfig:
    start_urls: List[str]
    item_selector: str
    name_selector: str
    price_selector: str

    # Optional fields you can use in YAML
    brand_selector: Optional[str] = None
    link_selector: Optional[str] = None
    next_page_selector: Optional[str] = None  # fallback if site has classic "Next" link

    # If the value is in an attribute instead of text
    name_attr: Optional[str] = None
    price_attr: Optional[str] = None

    # Any extra CSS -> column maps you want to scrape
    extra_fields: Dict[str, str] = field(default_factory=dict)

    # Safety limits
    max_pages: Optional[int] = None


# ----------------------------
# Price cleaning (AR formats)
# ----------------------------
PRICE_SYM_RE = re.compile(r"[$€£]|ARS|AR\$|USD", re.I)
NUM_RE = re.compile(r"(\d[\d\.\,]*)")

def clean_price(price_text: str) -> Tuple[Optional[str], Optional[float]]:
    """
    Extract currency symbol (if any) and numeric value.
    Handles formats like "$ 44.500" or "$ 35.600,50".
    We interpret '.' as thousands and ',' as decimal when both appear.
    If only '.' appears, we treat it as thousands (AR format).
    """
    if not price_text:
        return None, None

    currency = None
    sym = PRICE_SYM_RE.search(price_text)
    if sym:
        currency = sym.group(0)

    m = NUM_RE.search(price_text.replace("\u00a0", " "))
    if not m:
        return currency, None

    raw = m.group(1)

    # Heuristics:
    #   "44.500" -> 44500.0
    #   "35.600,50" -> 35600.50
    #   "124,99" -> 124.99
    if "," in raw and "." in raw:
        # thousands '.' and decimal ','
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw and "." not in raw:
        # likely decimal comma
        raw = raw.replace(".", "").replace(",", ".")
    else:
        # only dots: treat as thousands
        raw = raw.replace(".", "")

    try:
        return currency, float(raw)
    except ValueError:
        return currency, None

# ----------------------------
# Product scraper
# ----------------------------
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}


def fetch_with_playwright(url: str, wait_state: str = "networkidle") -> Optional[str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        logging.error("Playwright not installed. Run: pip install playwright && python -m playwright install")
        return None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_load_state(wait_state)
            return page.content()
        finally:
            browser.close()


class ProductScraper:
    def __init__(self, config_path: str, args):
        self.args = args
        with open(config_path, "r", encoding="utf-8") as f:
            raw_cfg = yaml.safe_load(f)
        # Build site configs
        self.configs: Dict[str, SiteConfig] = {k: SiteConfig(**v) for k, v in raw_cfg.items()}

        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def allowed_by_robots(self, url: str) -> bool:
        if self.args.no_robots:
            return True
        rp = get_robot_parser(url)
        if not rp:
            return True
        return rp.can_fetch(self.session.headers.get("User-Agent", "*"), url)

    def polite_sleep(self):
        lo, hi = self.args.delay
        if hi <= 0:
            return
        time.sleep(random.uniform(lo, hi))

    def fetch(self, url: str) -> Optional[str]:
        if not self.allowed_by_robots(url):
            logging.warning(f"Blocked by robots.txt: {url}")
            return None
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code >= 400:
                logging.warning(f"HTTP {resp.status_code} for {url}")
                return None
            return resp.text
        except requests.RequestException as e:
            logging.warning(f"Request failed for {url}: {e}")
            return None

    def parse_products_from_html(self, site_key: str, html: str, page_url: str, site: SiteConfig) -> List[Dict]:
        soup = BeautifulSoup(html, "html.parser")
        items = soup.select(site.item_selector)
        out: List[Dict] = []

        for item in items:
            # -------- BRAND (optional) --------
            brand_text = ""
            if site.brand_selector:
                be = item.select_one(site.brand_selector)
                if be:
                    brand_text = be.get_text(strip=True)

            # -------- Fallback if brand not found --------
            if site.brand_selector:
                be = item.select_one(site.brand_selector)
                if be:
                    brand_text = be.get_text(strip=True)
        

            # -------- NAME --------
            name_text = ""
            ne = item.select_one(site.name_selector)
            if ne:
                if site.name_attr:
                    name_text = (ne.get(site.name_attr) or "").strip()
                else:
                    name_text = ne.get_text(strip=True)

            # Combine brand + name with a space if brand exists
            full_name = f"{brand_text} {name_text}".strip()

            # -------- PRICE --------
            pe = item.select_one(site.price_selector)
            if not pe:
                # Try a stronger fallback: if price_selector has multiple options comma-separated,
                # BeautifulSoup already handled it; otherwise, skip item.
                continue

            if site.price_attr:
                price_text = (pe.get(site.price_attr) or "").strip()
            else:
                # If the selector returns a wrapper (e.g., spans inside), get all text
                price_text = pe.get_text(" ", strip=True)

            currency, price_val = clean_price(price_text)

            # -------- PRODUCT URL (optional) --------
            url_val = None
            if site.link_selector:
                le = item.select_one(site.link_selector)
                href = le.get("href") if le else None
                if href:
                    url_val = urljoin(page_url, href)

            # -------- EXTRAS --------
            extras = {}
            for col, css in (site.extra_fields or {}).items():
                if not css:
                    extras[col] = ""
                    continue
                ex = item.select_one(css)
                extras[col] = ex.get_text(strip=True) if ex else ""

            out.append(
                {
                    "site_key": site_key,
                    "source_url": page_url,
                   # "url": url_val or "",
                    "brand": brand_text,
                    "name": full_name or name_text,
                    "price_text": price_text,
                    "currency": currency or "",
                   # "price_value": price_val if price_val is not None else "",
                    **extras,
                }
            )

        return out

    def scrape_site(self, site_key: str, site: SiteConfig) -> List[Dict]:
        logging.info(f"Scraping site: {site_key}")
        rows: List[Dict] = []

        for base_url in site.start_urls:
            max_pages = self.args.max_pages or site.max_pages or 1

            for page_idx in range(1, max_pages + 1):
                # Prefer explicit ?page=N pagination (fast + no JS)
                paged_url = base_url
                sep = "&" if "?" in base_url else "?"
                # Only append ?page= if the base URL doesn't already have a page param
                if "page=" not in base_url.lower():
                    paged_url = f"{base_url}{sep}page={page_idx}"

                html = self.fetch(paged_url)
                if not html:
                    break

                # If we suspect JS-injected prices, try Playwright
                need_js = False

                if self.args.save_html:
                    # quick check: if no "$" in the HTML, likely no prices yet
                    if "$" not in html:
                        need_js = True
                else:
                    # Or decide via a cheap probe: if item cards exist but price selector finds zero in raw HTML
                    soup_probe = BeautifulSoup(html, "html.parser")
                    cards_cnt = len(soup_probe.select(site.item_selector))
                    prices_cnt = len(soup_probe.select(site.price_selector)) if site.price_selector else 0
                    if cards_cnt > 0 and prices_cnt == 0:
                        need_js = True

                if need_js:
                    print("DEBUG: Switching to Playwright for", paged_url)
                    html_js = fetch_with_playwright(paged_url, wait_state="networkidle")
                    if html_js:
                        html = html_js


                # ✅ Debug selector counts on the first page only
                if page_idx == 1:  # only run once per category to avoid spam
                    soup = BeautifulSoup(html, "html.parser")
                    probes = [
                        (f"item_selector ({site.item_selector})", site.item_selector),
                        ("alt item .vtex-product-summary-2-x-container", ".vtex-product-summary-2-x-container"),
                        ("name .vtex-product-summary-2-x-nameContainer", ".vtex-product-summary-2-x-nameContainer"),
                        ("price default", "span.vtex-product-price-1-x-sellingPriceValue, span.vtex-product-price-1-x-currencyContainer"),
                        ("price generic", "span[class*='BestPrice'], span[class*='price'], span[class*='currency']"),
                    ]
                    for label, css in probes:
                        try:
                            cnt = len(soup.select(css))
                            print(f"DEBUG: {label} -> {cnt}")
                        except Exception:
                            pass

                # Optional debug: write the last fetched page to disk
                if self.args.save_html:
                    with open("last_page.html", "w", encoding="utf-8") as _f:
                        _f.write(html)

                # Parse items on this page
                page_rows = self.parse_products_from_html(site_key, html, paged_url, site)
                if not page_rows and page_idx == 1 and site.next_page_selector:
                    # If selectors returned nothing, try a "next page" fallback on the first page
                    # (useful for non-?page sites)
                    soup = BeautifulSoup(html, "html.parser")
                    next_link = soup.select_one(site.next_page_selector)
                    if next_link and next_link.get("href"):
                        next_url = urljoin(paged_url, next_link.get("href"))
                        html2 = self.fetch(next_url)
                        if html2:
                            if self.args.save_html:
                                with open("last_page.html", "w", encoding="utf-8") as _f:
                                    _f.write(html2)
                            page_rows = self.parse_products_from_html(site_key, html2, next_url, site)

                rows.extend(page_rows)

                # Stop early if this page had 0 items (likely no more pages)
                if not page_rows:
                    break

                self.polite_sleep()

        return rows

    def write_csv(self, site_key: str, rows: List[Dict]):
        out_name = f"products_{site_key}.csv"
        if not rows:
            logging.info(f"Wrote {out_name} (0 rows)")
            with open(out_name, "w", newline="", encoding="utf-8") as f:
                f.write("")  # empty file, still created
            return

        # Build headers from all keys seen (stable order for common fields)
        base_cols = ["site_key", "source_url", "url", "brand", "name", "currency", "price_value", "price_text"]
        extra_cols = []
        for r in rows:
            for k in r.keys():
                if k not in base_cols and k not in extra_cols:
                    extra_cols.append(k)
        headers = base_cols + [c for c in extra_cols if c not in base_cols]

        with open(out_name, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

        logging.info(f"Wrote {out_name} ({len(rows)} rows)")

# ----------------------------
# CLI
# ----------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Simple YAML-driven product scraper")
    p.add_argument("--site", required=True, help="Site key from config_sites.yaml")
    p.add_argument("--config", default="config_sites.yaml", help="Path to YAML config")
    p.add_argument("--max-pages", type=int, default=None, help="Override max_pages from YAML")
    p.add_argument("--delay", nargs=2, type=float, default=[1.0, 2.5], metavar=("MIN", "MAX"),
                   help="Random delay range between requests (seconds). Use 0 0 to disable.")
    p.add_argument("--no-robots", action="store_true", help="Ignore robots.txt (use only if you have permission)")
    p.add_argument("--save-html", action="store_true", help="Save last fetched page as last_page.html for debugging")
    return p.parse_args()

def main():
    args = parse_args()
    scraper = ProductScraper(args.config, args)

    site_key = args.site
    if site_key not in scraper.configs:
        logging.error(f"Site '{site_key}' not found in {args.config}")
        return

    site = scraper.configs[site_key]
    rows = scraper.scrape_site(site_key, site)
    scraper.write_csv(site_key, rows)

if __name__ == "__main__":
    main()

