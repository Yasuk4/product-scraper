# Product Scraper 

This is a configurable scraper for collecting product **name** and **price** from specific pages.

> ⚠️ Always check (and follow) the site's Terms of Service and `robots.txt`. Get permission when in doubt.

## Quick Start (Windows 11 friendly)

1. Install Python 3.11+ from python.org and ensure `python` and `pip` work in PowerShell.
2. In this folder:
   ```powershell
   pip install -r requirements.txt
   python -m playwright install chromium
   ```
3. Edit `config_sites.yaml`:
   - Put your store pages under `start_urls`
   - Update the CSS selectors (`item_selector`, `name_selector`, `price_selector`, `next_page_selector`)
4. Run it:
   ```powershell
   python scraper.py --site demo_store
   ```
   Or scrape every configured site:
   ```powershell
   python scraper.py --all
   ```
5. Check the output:
   - `products_demo_store.csv`
   - (If `--all`) `products_combined.csv`

## Flags

| Flag               | Example                 | Description                                                                   |
| ------------------ | ----------------------- | ----------------------------------------------------------------------------- |
| `--site`           | `--site pigmento_store` | **(Required)** – Selects which configuration from `config_sites.yaml` to use. |
| `--max-pages`      | `--max-pages 10`        | Limits the number of pages scraped per category.                              |
| `--delay`          | `--delay 1 3`           | Adds a random delay between 1–3 seconds per page request.                     |
| `--save-html`      | `--save-html`           | Saves the last fetched page as `last_page.html` for debugging.                |
| `--output`         | `--output custom.csv`   | Sets a custom name for the CSV output file.                                   |
| `--enrich-brand`   | `--enrich-brand`        | (Optional) Visits product pages to fill in missing brand info.                |
| `--use-playwright` | `--use-playwright`      | Forces JavaScript rendering with Playwright.                                  |
| `--debug`          | `--debug`               | Prints detailed logs for troubleshooting selectors.                           |

## Finding the Right Selectors

Open a product listing page in Chrome → Right-click → **Inspect**. Identify:
- The container for a single product card (e.g., `.product-card`)
- Inside it, the name element (e.g., `.product-title`)
- Inside it, the price element (e.g., `.price`)
- The "next page" link (e.g., `a.next`) for pagination

Paste these into `config_sites.yaml`.

### Attribute-based Content
If the name/price is in an attribute instead of text, set `name_attr`/`price_attr` (e.g., `data-price`) and keep the CSS selector pointing at that node.

## JS-Heavy Pages (Dynamic Content)

If the page needs JavaScript to render products, switch to **Playwright**:

```powershell
pip install playwright
python -m playwright install
```

Then replace the `fetch()` with a Playwright page loader that:
- `page.goto(url, wait_until="networkidle")`
- `page.content()` to get HTML
- Continue using the same parsing functions.

(Keeping the rest of the pipeline identical.)

## Politeness & Anti-blocking

- The script rotates User-Agents and sleeps **1.0–2.5s** between pages by default.
- You can change delay: `--delay 2 5`.
- Retries/backoff are minimal to keep it simple—add `tenacity` if needed.
- Consider using your own IP, not a shared VPN. Respect rate limits.

## Prices

We extract a currency (symbol or code) if present and parse numeric value with a couple of locale heuristics. The raw `price_text` is also kept in the CSV in case parsing fails.

## CSV Columns

- `site_key, name, currency, price_value, price_text, source_url` + any `extra_fields` you add.

## Common Pitfalls

- Wrong selectors → zero rows.
- JS-only content → needs Playwright.
- Geo/IP gates → may need to run from the right region or log in (only if allowed).
- Terms of Service → make sure scraping is permitted for your use-case.
