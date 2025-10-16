#!/usr/bin/env python3
"""
Configurable Web Scraper for Product Name & Price
-------------------------------------------------
- Works with static pages (server-rendered HTML). For JS-heavy sites, see README notes to switch to Playwright.
- Configure each site in config_sites.yaml with CSS selectors.
- Outputs one CSV per site and a combined CSV.
- Simple politeness: random delay, rotating User-Agents, optional robots.txt check.
- Pagination supported via a "next page" CSS selector.

Usage:
  python scraper.py --site demo_store
  python scraper.py --all
  python scraper.py --site demo_store --max-pages 5 --delay 1.0 2.5

Requires:
  Python 3.9+
  pip install -r requirements.txt
"""
import argparse
import csv
import dataclasses
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import yaml

# --------------- Helpers ---------------

USER_AGENTS = [
    # A small rotating pool (feel free to extend)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]

PRICE_RE = re.compile(r"([$\€\£\¥\₽\₩\₹\₱\₫\₴\₦]|ARS|USD|EUR)?\s*([0-9]+(?:[.,\s][0-9]{3})*(?:[.,][0-9]{2})?)", re.UNICODE)

def clean_price(text: str) -> Tuple[Optional[str], Optional[float]]:
    """
    Extracts currency symbol/code and a numeric value from a price-like string.
    Returns (currency, value) where value is float with '.' decimal separator.
    """
    if not text:
        return None, None
    m = PRICE_RE.search(text.replace("\u00A0", " "))
    if not m:
        return None, None
    currency = m.group(1).strip() if m.group(1) else None
    raw = m.group(2)
    # Heuristic: if both ',' and '.' appear, assume ',' are thousands and '.' decimal if '.' is the last separator, else vice versa.
    if ',' in raw and '.' in raw:
        if raw.rfind('.') > raw.rfind(','):
            num = raw.replace(',', '')
        else:
            num = raw.replace('.', '').replace(',', '.')
    elif ',' in raw and raw.count(',') > 1:
        num = raw.replace(',', '')
    elif ',' in raw:
        # assume comma is decimal
        num = raw.replace(',', '.')
    else:
        num = raw
    try:
        return currency, float(num)
    except ValueError:
        return currency, None

def politeness_delay(min_s: float, max_s: float):
    time.sleep(random.uniform(min_s, max_s))

def get_robot_parser(base_url: str):
    # Minimal robots.txt checker (best-effort). If it fails, default to allowed.
    try:
        from urllib import robotparser
        rp = robotparser.RobotFileParser()
        robots_url = urljoin(base_url, "/robots.txt")
        rp.set_url(robots_url)
        rp.read()
        return rp
    except Exception:
        return None

# --------------- Data Models ---------------

@dataclass
class SiteConfig:
    start_urls: List[str]
    item_selector: str
    name_selector: str
    price_selector: str
    next_page_selector: Optional[str] = None
    name_attr: Optional[str] = None
    price_attr: Optional[str] = None
    extra_fields: Dict[str, str] = field(default_factory=dict)  # {field_name: css_selector}
    max_pages: Optional[int] = None

@dataclass
class Product:
    site_key: str
    source_url: str
    name: str
    price_text: str
    currency: Optional[str]
    price_value: Optional[float]
    extra: Dict[str, str] = field(default_factory=dict)

# --------------- Core Scraper ---------------

class ProductScraper:
    def __init__(self, cfg_path: str, min_delay: float = 1.0, max_delay: float = 2.5,
                 obey_robots: bool = True, timeout: float = 20.0):
        with open(cfg_path, "r", encoding="utf-8") as f:
            self.configs: Dict[str, SiteConfig] = {
                k: SiteConfig(**v) for k, v in yaml.safe_load(f).items()
            }
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.obey_robots = obey_robots
        self.timeout = timeout
        self.session = requests.Session()

    def fetch(self, url: str) -> Optional[str]:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        try:
            resp = self.session.get(url, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            logging.warning("Request failed for %s: %s", url, e)
            return None

    def allowed_by_robots(self, url: str) -> bool:
        if not self.obey_robots:
            return True
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        rp = get_robot_parser(base)
        if rp is None:
            return True
        ua = "Mozilla/5.0"  # generic ua
        try:
            return rp.can_fetch(ua, url)
        except Exception:
            return True

    def parse_products_from_html(self, html: str, site: SiteConfig, page_url: str) -> List[Product]:
        soup = BeautifulSoup(html, "lxml")
        out: List[Product] = []
        for item in soup.select(site.item_selector):
            # Name
            name_el = item.select_one(site.name_selector)
            if not name_el:
                continue
            name = name_el.get_text(strip=True) if not site.name_attr else name_el.get(site.name_attr, "").strip()

            # Price
            price_el = item.select_one(site.price_selector)
            if not price_el:
                continue
            price_text = price_el.get_text(strip=True) if not site.price_attr else price_el.get(site.price_attr, "").strip()
            currency, price_val = clean_price(price_text)

            # Extras
            extras = {}
            for field, css in site.extra_fields.items():
                el = item.select_one(css)
                extras[field] = el.get_text(strip=True) if el else ""

            out.append(Product(
                site_key="",  # filled later by caller
                source_url=page_url,
                name=name,
                price_text=price_text,
                currency=currency,
                price_value=price_val,
                extra=extras
            ))
        return out

    def find_next_page(self, html: str, site: SiteConfig, current_url: str) -> Optional[str]:
        if not site.next_page_selector:
            return None
        soup = BeautifulSoup(html, "lxml")
        el = soup.select_one(site.next_page_selector)
        if not el:
            return None
        href = el.get("href") or el.get("data-href")
        if not href:
            return None
        return urljoin(current_url, href)

    def scrape_site(self, key: str, limit_pages: Optional[int] = None) -> List[Product]:
        if key not in self.configs:
            raise KeyError(f"Unknown site key: {key}")
        site = self.configs[key]
        results: List[Product] = []
        max_pages = limit_pages or site.max_pages
        visited = 0
        for start_url in site.start_urls:
            url = start_url
            while url:
                if not self.allowed_by_robots(url):
                    logging.info("Blocked by robots.txt: %s", url)
                    break
                html = self.fetch(url)
                if not html:
                    break
                items = self.parse_products_from_html(html, site, url)
                for p in items:
                    p.site_key = key
                results.extend(items)
                visited += 1
                if max_pages and visited >= max_pages:
                    break
                nxt = self.find_next_page(html, site, url)
                if nxt and nxt != url:
                    politeness_delay(self.min_delay, self.max_delay)
                    url = nxt
                else:
                    break
        return results

# --------------- CLI ---------------

def write_csv(path: str, products: List[Product]):
    fieldnames = ["site_key", "name", "currency", "price_value", "price_text", "source_url"]
    # Include extras dynamically
    extra_keys = sorted({k for p in products for k in p.extra.keys()})
    fieldnames.extend(extra_keys)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for p in products:
            row = {
                "site_key": p.site_key,
                "name": p.name,
                "currency": p.currency or "",
                "price_value": f"{p.price_value:.2f}" if p.price_value is not None else "",
                "price_text": p.price_text,
                "source_url": p.source_url,
            }
            for k in extra_keys:
                row[k] = p.extra.get(k, "")
            w.writerow(row)

def main():
    parser = argparse.ArgumentParser(description="Web scraper for product name & price (static HTML).")
    parser.add_argument("--config", default="config_sites.yaml", help="YAML config with site definitions")
    parser.add_argument("--site", help="Site key to scrape (as defined in YAML)")
    parser.add_argument("--all", action="store_true", help="Scrape all configured sites")
    parser.add_argument("--max-pages", type=int, help="Max pages per site (overrides per-site max)")
    parser.add_argument("--delay", nargs=2, type=float, metavar=("MIN", "MAX"),
                        help="Random delay range in seconds (default 1.0 2.5)")
    parser.add_argument("--no-robots", action="store_true", help="Ignore robots.txt (not recommended)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    min_delay, max_delay = (1.0, 2.5)
    if args.delay:
        min_delay, max_delay = args.delay

    scraper = ProductScraper(
        cfg_path=args.config,
        min_delay=min_delay,
        max_delay=max_delay,
        obey_robots=not args.no_robots,
    )

    if not args.site and not args.all:
        parser.error("You must provide --site KEY or --all")

    site_keys = list(scraper.configs.keys()) if args.all else [args.site]
    combined: List[Product] = []
    for key in site_keys:
        logging.info("Scraping site: %s", key)
        products = scraper.scrape_site(key, limit_pages=args.max_pages)
        out_csv = f"products_{key}.csv"
        write_csv(out_csv, products)
        logging.info("Wrote %s (%d rows)", out_csv, len(products))
        combined.extend(products)

    if len(site_keys) > 1:
        write_csv("products_combined.csv", combined)
        logging.info("Wrote products_combined.csv (%d rows)", len(combined))

if __name__ == "__main__":
    main()
