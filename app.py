#!/usr/bin/env python3
"""
POKÉMON TCG — English 30th Anniversary / Celebration Monitor

Catalog/search-page monitor only:
- Does not rely on hardcoded individual product URLs.
- Sends Discord alerts for NEW LISTINGS and RESTOCKS.
- Preserves stock state when a site returns an error, Cloudflare, CAPTCHA,
  access denial, or a page without catalog evidence.
- Includes detailed verification logging for every configured store.

Required environment variable in .env:
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

Optional environment variables:
SCAN_SECONDS=65
STATE_FILE=productos_estado_english_catalog.json
RUN_ONCE=1
VERIFY_ONLY=1
HEADLESS=true
"""

import asyncio
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page, async_playwright


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
SCAN_SECONDS = max(30, int(os.getenv("SCAN_SECONDS", "65")))
STATE_FILE = Path(
    os.getenv("STATE_FILE", "productos_estado_english_catalog.json")
)
RUN_ONCE = os.getenv("RUN_ONCE", "").strip().lower() in {"1", "true", "yes"}
VERIFY_ONLY = os.getenv("VERIFY_ONLY", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
HEADLESS = os.getenv("HEADLESS", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}

PAGE_TIMEOUT_MS = 30_000
POST_LOAD_WAIT_MS = 4_500
MAX_CARD_TEXT = 3_500

# Broad monitoring: all English Pokémon 30th Anniversary / Celebration products.
MONITOR_ALL_ENGLISH_30TH_PRODUCTS = True


@dataclass(frozen=True)
class Store:
    name: str
    search_url: str


# All monitoring is from search/catalog pages. No product URLs are hardcoded.
STORES = [
    Store(
        "Plaza Vea",
        "https://www.plazavea.com.pe/pokemon-tcg?PS=50",
    ),
    Store(
        "Saga Falabella",
        "https://www.falabella.com.pe/falabella-pe/search?Ntt=pokemon%2030%20aniversario",
    ),
    Store(
        "Ripley",
        "https://simple.ripley.com.pe/search/pokemon%2030%20aniversario",
    ),
    Store(
        "Tai Loy",
        "https://www.tailoy.com.pe/catalogsearch/result/?q=pokemon+30+aniversario",
    ),
    Store(
        "Phantom",
        "https://phantom.pe/catalogsearch/result/?q=pokemon+30+aniversario",
    ),
    Store(
        "LawGamers",
        "https://lawgamers.com/?s=pokemon+30+aniversario&post_type=product",
    ),
    Store(
        "Oechsle",
        "https://www.oechsle.pe/search/?text=pokemon%2030%20aniversario",
    ),
    Store(
        "Metro",
        "https://www.metro.pe/pokemon-tcg?PS=50",
    ),
    Store(
        "Wong",
        "https://www.wong.pe/pokemon-tcg?PS=50",
    ),
    Store(
        "Pharmax",
        "https://pharmax.com.pe/search?q=30%20aniversario*&type=product",
    ),
    Store(
        "Ilahui",
        "https://ilahuiperu.com/search?options%5Bunavailable_products%5D=last&options%5Bprefix%5D=last&options%5Bfields%5D=title%2Cvendor%2Cproduct_type%2Cvariants.title&q=pokemon+30",
    ),
]


# ---------------------------------------------------------------------------
# Text, product, and stock helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize(value: Any) -> str:
    value = clean_text(value)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return value.lower()


def canonical_url(url: str) -> str:
    """Keeps product identity stable if catalog URLs add tracking parameters."""
    try:
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except Exception:
        return url


def product_key(store_name: str, url: str) -> str:
    return f"{store_name}|{canonical_url(url)}"


def is_target_product(text: str) -> bool:
    """
    Broad target:
      Pokémon + 30th Anniversary / Celebration + English.

    Examples accepted:
      - Pokémon TCG 30th Celebration Mini Tin (Inglés)
      - Colección con Póster POKÉMON TCG 30.º Aniversario en Inglés
      - 3 Pack TCG Cartas Pokemon 30 aniversario Ingles
    """
    text = normalize(text)

    has_pokemon = "pokemon" in text

    has_30th = bool(
        re.search(
            r"\b30\s*(?:\.|o|º|°)?\s*(?:th\s*)?"
            r"(?:aniversario|anniversary|celebration)\b",
            text,
        )
    )

    has_english = bool(
        re.search(
            r"\b(?:ingles|english)\b",
            text,
        )
    )

    return has_pokemon and has_30th and has_english


def is_sold_out(text: str) -> bool:
    text = normalize(text)
    sold_out_markers = [
        "agotado",
        "sin stock",
        "no disponible",
        "not available",
        "out of stock",
        "sold out",
        "temporalmente no disponible",
        "producto no disponible",
    ]
    return any(marker in text for marker in sold_out_markers)


def has_price(text: str) -> bool:
    text = normalize(text)
    return bool(
        re.search(
            r"(?:s\/|s\.\s*|precio\s*:?\s*|pen\s*)\s*\d+(?:[.,]\d{1,2})?",
            text,
        )
    )


def extract_price(text: str) -> str:
    original = clean_text(text)

    patterns = [
        r"(S\/\s*\d+(?:[.,]\d{1,2})?)",
        r"(S\.\s*\d+(?:[.,]\d{1,2})?)",
        r"(PEN\s*\d+(?:[.,]\d{1,2})?)",
    ]

    for pattern in patterns:
        match = re.search(pattern, original, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(1))

    return ""


def is_product_like_url(url: str) -> bool:
    """Recognizes common product URL structures without fixed product URLs."""
    parsed = urlsplit(url)
    path = parsed.path.lower()

    product_patterns = [
        "/products/",
        "/product/",
        "/producto/",
        "/p/",
        "/p?",
        "/item/",
        "/sku/",
        ".html",
    ]

    return any(pattern in path for pattern in product_patterns)


def page_looks_blocked(body_text: str, page_title: str) -> bool:
    text = normalize(f"{page_title} {body_text[:20_000]}")

    blocked_markers = [
        "access denied",
        "error 403",
        "forbidden",
        "just a moment",
        "checking your browser",
        "cloudflare",
        "captcha",
        "verify you are human",
        "attention required",
        "request blocked",
        "temporarily unavailable",
    ]

    return any(marker in text for marker in blocked_markers)


def page_has_explicit_no_results(body_text: str) -> bool:
    text = normalize(body_text)

    empty_markers = [
        "no se encontraron productos",
        "no encontramos productos",
        "no se encontraron resultados",
        "sin resultados",
        "no results found",
        "your search did not match any products",
    ]

    return any(marker in text for marker in empty_markers)


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "version": 2,
            "products": {},
            "created_at": now_iso(),
        }

    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as error:
        print(f"State file could not be read; preserving it and starting safely: {error}")
        return {
            "version": 2,
            "products": {},
            "created_at": now_iso(),
        }

    if not isinstance(loaded, dict):
        return {
            "version": 2,
            "products": {},
            "created_at": now_iso(),
        }

    if not isinstance(loaded.get("products"), dict):
        # Do not delete an older state file. The monitor simply starts a new
        # compatible state structure in the same file on the next save.
        loaded["products"] = {}

    loaded["version"] = 2
    return loaded


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    temporary_file = STATE_FILE.with_suffix(f"{STATE_FILE.suffix}.tmp")

    temporary_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary_file.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def discord_post(payload: dict[str, Any]) -> tuple[bool, str]:
    if not DISCORD_WEBHOOK_URL:
        return False, "DISCORD_WEBHOOK_URL is not configured"

    request = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if 200 <= response.status < 300:
                return True, f"HTTP {response.status}"
            return False, f"HTTP {response.status}"

    except urllib.error.HTTPError as error:
        return False, f"Discord HTTP {error.code}"

    except Exception as error:
        return False, str(error)


async def send_discord_alert(product: dict[str, Any]) -> bool:
    alert_type = product.get("alert_type", "IN STOCK")
    title = clean_text(product.get("title", "Pokémon TCG listing"))
    store = clean_text(product.get("store", "Unknown store"))
    url = clean_text(product.get("url", ""))
    price = clean_text(product.get("price", ""))

    description_lines = [
        f"**Store:** {store}",
        f"**Product:** {title}",
    ]

    if price:
        description_lines.append(f"**Price:** {price}")

    description_lines.extend(
        [
            "**Language:** English",
            "",
            url,
        ]
    )

    payload = {
        "username": "Pokémon TCG Stock Monitor",
        "embeds": [
            {
                "title": f"{alert_type}: English Pokémon 30th Product",
                "description": "\n".join(description_lines)[:4000],
                "color": 0x57F287 if alert_type == "RESTOCK" else 0xFEE75C,
                "footer": {
                    "text": "Catalog/search-page monitor — verify checkout availability immediately"
                },
                "timestamp": now_iso(),
            }
        ],
    }

    success, detail = await asyncio.to_thread(discord_post, payload)

    if success:
        print(f"Discord alert delivered for {store}: {title}")
    else:
        print(f"Discord alert failed for {store}: {title} | {detail}")

    return success


# ---------------------------------------------------------------------------
# Playwright catalog extraction
# ---------------------------------------------------------------------------

ANCHOR_EXTRACTION_JS = r"""
anchors => anchors.slice(0, 5000).map(anchor => {
    const card =
        anchor.closest(
            "article, li, [data-testid*='product'], [class*='product-card'], " +
            "[class*='ProductCard'], [class*='product-item'], [class*='ProductItem']"
        ) || anchor.parentElement;

    const text = (anchor.innerText || anchor.textContent || "").trim();
    const parentText = card
        ? (card.innerText || card.textContent || "").trim()
        : "";

    return {
        href: anchor.href || "",
        text,
        title: anchor.getAttribute("title") || "",
        ariaLabel: anchor.getAttribute("aria-label") || "",
        parentText: parentText.slice(0, 5000)
    };
});
"""

CARD_EXTRACTION_JS = r"""
cards => cards.slice(0, 1500).map(card => {
    const anchor = card.querySelector("a[href]");
    if (!anchor) return null;

    return {
        href: anchor.href || "",
        text: (anchor.innerText || anchor.textContent || "").trim(),
        title: anchor.getAttribute("title") || "",
        ariaLabel: anchor.getAttribute("aria-label") || "",
        parentText: (card.innerText || card.textContent || "").trim().slice(0, 5000)
    };
}).filter(Boolean);
"""

CARD_SELECTOR = (
    "article, "
    "[data-testid*='product'], "
    "[class*='product-card'], "
    "[class*='ProductCard'], "
    "[class*='product-item'], "
    "[class*='ProductItem']"
)


async def safely_scroll_for_lazy_products(page: Page) -> None:
    """
    Gives catalog pages a chance to load cards without clicking, logging in,
    bypassing controls, or attempting to defeat site access protections.
    """
    for _ in range(3):
        await page.evaluate(
            "() => window.scrollTo(0, document.body.scrollHeight)"
        )
        await page.wait_for_timeout(700)

    await page.evaluate("() => window.scrollTo(0, 0)")
    await page.wait_for_timeout(300)


def candidate_context(candidate: dict[str, Any]) -> str:
    return clean_text(
        " | ".join(
            [
                clean_text(candidate.get("text", "")),
                clean_text(candidate.get("title", "")),
                clean_text(candidate.get("ariaLabel", "")),
                clean_text(candidate.get("parentText", "")),
            ]
        )
    )[:MAX_CARD_TEXT]


def extract_target_products(
    raw_candidates: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """
    Returns matching product-card records.

    The matching text is taken from the product card / parent container rather
    than only anchor text. This is essential for stores that place the name,
    language, price, and stock label in separate HTML elements.
    """
    products_by_url: dict[str, dict[str, str]] = {}

    for candidate in raw_candidates:
        url = clean_text(candidate.get("href", ""))

        if not url.startswith(("https://", "http://")):
            continue

        if url.endswith("#"):
            continue

        context = candidate_context(candidate)

        if not is_target_product(context):
            continue

        # Avoid treating a navigation/category link as a product merely because
        # a large page container happened to include matching words.
        if not is_product_like_url(url) and not has_price(context):
            continue

        title = clean_text(
            candidate.get("title")
            or candidate.get("text")
            or candidate.get("ariaLabel")
            or "Pokémon TCG 30th product"
        )

        # Parent/card text often contains the true complete product title.
        # Prefer it if the anchor title was generic or incomplete.
        parent_text = clean_text(candidate.get("parentText", ""))
        if (
            len(parent_text) > len(title)
            and is_target_product(parent_text)
            and len(parent_text) < 700
        ):
            title = parent_text

        key = canonical_url(url)
        product = {
            "url": url,
            "title": title[:700],
            "price": extract_price(context),
            "available": not is_sold_out(context),
        }

        existing = products_by_url.get(key)
        if existing is None:
            products_by_url[key] = product
            continue

        # Keep the richer version if duplicate selectors find the same product.
        if len(product["title"]) > len(existing["title"]):
            products_by_url[key] = product
        elif product["price"] and not existing["price"]:
            products_by_url[key] = product

    return list(products_by_url.values())


async def scrape_store(
    context: BrowserContext,
    store: Store,
) -> dict[str, Any]:
    page = await context.new_page()

    try:
        response = await page.goto(
            store.search_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )

        status = response.status if response else 0

        await page.wait_for_timeout(POST_LOAD_WAIT_MS)
        await safely_scroll_for_lazy_products(page)

        final_url = page.url
        page_title = clean_text(await page.title())

        body_text = clean_text(
            await page.locator("body").inner_text(timeout=10_000)
        )

        anchor_locator = page.locator("a[href]")
        anchor_count = await anchor_locator.count()

        raw_anchors = await anchor_locator.evaluate_all(ANCHOR_EXTRACTION_JS)

        card_locator = page.locator(CARD_SELECTOR)
        card_count = await card_locator.count()

        raw_cards: list[dict[str, Any]] = []
        if card_count:
            raw_cards = await card_locator.evaluate_all(CARD_EXTRACTION_JS)

        raw_candidates = raw_anchors + raw_cards

        product_link_count = sum(
            1
            for candidate in raw_candidates
            if is_product_like_url(clean_text(candidate.get("href", "")))
        )

        target_products = extract_target_products(raw_candidates)
        explicit_no_results = page_has_explicit_no_results(body_text)

        if status >= 400:
            return {
                "store": store,
                "verified": False,
                "reason": f"HTTP {status} returned by store",
                "status": status,
                "final_url": final_url,
                "title": page_title,
                "anchors": anchor_count,
                "cards": card_count,
                "product_links": product_link_count,
                "targets": [],
            }

        if page_looks_blocked(body_text, page_title):
            return {
                "store": store,
                "verified": False,
                "reason": "access/error page detected (Cloudflare, CAPTCHA, denial, or temporary error)",
                "status": status,
                "final_url": final_url,
                "title": page_title,
                "anchors": anchor_count,
                "cards": card_count,
                "product_links": product_link_count,
                "targets": [],
            }

        # This prevents the original Plaza Vea autocomplete-only page problem:
        # many suggestion links alone are not accepted as catalog proof.
        has_catalog_evidence = (
            product_link_count > 0
            or len(raw_cards) > 0
            or explicit_no_results
        )

        if not has_catalog_evidence:
            return {
                "store": store,
                "verified": False,
                "reason": "page loaded but contained no product-card/product-link evidence",
                "status": status,
                "final_url": final_url,
                "title": page_title,
                "anchors": anchor_count,
                "cards": card_count,
                "product_links": product_link_count,
                "targets": [],
            }

        return {
            "store": store,
            "verified": True,
            "reason": (
                "explicit no-results page"
                if explicit_no_results
                else "catalog evidence found"
            ),
            "status": status,
            "final_url": final_url,
            "title": page_title,
            "anchors": anchor_count,
            "cards": card_count,
            "product_links": product_link_count,
            "targets": target_products,
        }

    except Exception as error:
        return {
            "store": store,
            "verified": False,
            "reason": f"scrape exception: {type(error).__name__}: {error}",
            "status": 0,
            "final_url": page.url,
            "title": "",
            "anchors": 0,
            "cards": 0,
            "product_links": 0,
            "targets": [],
        }

    finally:
        await page.close()


# ---------------------------------------------------------------------------
# Monitoring and restock logic
# ---------------------------------------------------------------------------

def print_verification(result: dict[str, Any]) -> None:
    store: Store = result["store"]
    verification = "PASS" if result["verified"] else "PRESERVE STATE"

    print(
        f"{store.name}: [VERIFY {verification}] "
        f"HTTP {result['status']} | "
        f"anchors={result['anchors']} | "
        f"cards={result['cards']} | "
        f"product-links={result['product_links']} | "
        f"matching-targets={len(result['targets'])} | "
        f"reason={result['reason']} | "
        f"final-url={result['final_url']}"
    )


def update_state_from_result(
    state: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """
    A verified catalog result can:
      - create a newly listed product;
      - mark a missing product unavailable;
      - detect a restock after an unavailable/missing state.

    An unverified result never changes availability state.
    """
    if not result["verified"]:
        return

    store: Store = result["store"]
    products_state: dict[str, Any] = state["products"]
    observed_keys: set[str] = set()

    for listing in result["targets"]:
        key = product_key(store.name, listing["url"])
        observed_keys.add(key)

        available = bool(listing["available"])
        old = products_state.get(key)

        if old is None:
            products_state[key] = {
                "store": store.name,
                "url": listing["url"],
                "title": listing["title"],
                "price": listing["price"],
                "available": available,
                "first_seen": now_iso(),
                "last_seen": now_iso(),
                "last_status_change": now_iso(),
                "pending_alert": available,
                "alert_type": "NEW LISTING",
            }
            continue

        was_available = bool(old.get("available", False))

        old["store"] = store.name
        old["url"] = listing["url"]
        old["title"] = listing["title"]
        old["price"] = listing["price"] or old.get("price", "")
        old["last_seen"] = now_iso()
        old["available"] = available

        if available and not was_available:
            old["last_status_change"] = now_iso()
            old["pending_alert"] = True
            old["alert_type"] = "RESTOCK"

        elif not available and was_available:
            old["last_status_change"] = now_iso()
            old["pending_alert"] = False
            old["alert_type"] = ""

    # If a prior target product was not present in a successfully verified
    # catalog/search response, treat it as unavailable. Its reappearance later
    # will produce a RESTOCK alert.
    for key, old in products_state.items():
        if old.get("store") != store.name:
            continue

        if key not in observed_keys and old.get("available", False):
            old["available"] = False
            old["pending_alert"] = False
            old["alert_type"] = ""
            old["last_status_change"] = now_iso()
            old["last_missing_from_verified_catalog"] = now_iso()


async def dispatch_pending_alerts(state: dict[str, Any]) -> None:
    if VERIFY_ONLY:
        return

    products_state: dict[str, Any] = state["products"]

    for _, product in products_state.items():
        if not product.get("pending_alert", False):
            continue

        if not product.get("available", False):
            product["pending_alert"] = False
            continue

        delivered = await send_discord_alert(product)

        if delivered:
            product["pending_alert"] = False
            product["last_alert"] = now_iso()


async def run_scan_cycle(cycle_number: int) -> None:
    print(
        f"\n=== Starting scan cycle #{cycle_number} "
        f"[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] ==="
    )

    if VERIFY_ONLY:
        print("VERIFY_ONLY=1: no state will be changed and no Discord alerts will be sent.")

    state = load_state()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=HEADLESS)

        browser_context = await browser.new_context(
            viewport={"width": 1440, "height": 1100},
            locale="es-PE",
            timezone_id="America/Lima",
        )

        try:
            for store in STORES:
                result = await scrape_store(browser_context, store)
                print_verification(result)

                if result["verified"]:
                    for listing in result["targets"]:
                        availability = "IN STOCK" if listing["available"] else "SOLD OUT"
                        print(
                            f"  MATCH [{availability}] {listing['title']} | "
                            f"{listing['price'] or 'price not found'} | {listing['url']}"
                        )

                if not VERIFY_ONLY:
                    update_state_from_result(state, result)

        finally:
            await browser_context.close()
            await browser.close()

    if not VERIFY_ONLY:
        await dispatch_pending_alerts(state)
        save_state(state)

    print("=== Cycle complete. ===")


async def main() -> None:
    print("Pokémon English 30th Anniversary / Celebration Catalog Monitor")
    print(f"Configured stores: {len(STORES)}")
    print(f"State file: {STATE_FILE}")
    print(f"Scan interval: {SCAN_SECONDS} seconds")
    print(f"Run once: {RUN_ONCE}")
    print(f"Verification only: {VERIFY_ONLY}")

    if not VERIFY_ONLY and not DISCORD_WEBHOOK_URL:
        print(
            "WARNING: DISCORD_WEBHOOK_URL is missing. "
            "The monitor will scan, but pending alerts cannot be delivered."
        )

    cycle_number = 1

    while True:
        try:
            await run_scan_cycle(cycle_number)
        except Exception as error:
            print(
                f"Unexpected scan-cycle failure: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
            )

        if RUN_ONCE:
            break

        cycle_number += 1
        print(f"Next scan in {SCAN_SECONDS} seconds.")
        await asyncio.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
