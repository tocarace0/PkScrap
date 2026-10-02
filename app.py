"""
POKÉMON TCG — English 30th Anniversary / Celebration Stock Monitor

Catalog/search-page monitor only:
- Searches multiple 30th Anniversary, 30.º, Celebration, English, and product-family terms.
- Does not use hardcoded individual product URLs.
- Sends Discord alerts for new available listings and restocks.
- Preserves stock state when searches are blocked, time out, redirect incorrectly,
  return access-error pages, or lack product-link/no-results evidence.

Required environment variable:
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

Optional environment variables:
SCAN_SECONDS=120
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
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from playwright.async_api import BrowserContext, Page, async_playwright


# ---------------------------------------------------------------------------
# Environment and configuration
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent

if load_dotenv is not None:
    load_dotenv(PROJECT_DIR / ".env")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
SCAN_SECONDS = max(60, int(os.getenv("SCAN_SECONDS", "120")))
STATE_FILE = PROJECT_DIR / os.getenv(
    "STATE_FILE",
    "productos_estado_english_catalog.json",
)

RUN_ONCE = os.getenv("RUN_ONCE", "").strip().lower() in {
    "1",
    "true",
    "yes",
}

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

PAGE_TIMEOUT_MS = 20_000
POST_LOAD_WAIT_MS = 2_000
SCROLL_WAIT_MS = 650
QUERY_DELAY_MS = 300
MAX_CARD_TEXT = 3_500


# ---------------------------------------------------------------------------
# Search terms
#
# Every store receives each search term using its own search URL format.
# The monitor then filters results for:
# Pokémon + 30th/30.º/Anniversary/Celebration + English/Inglés.
# ---------------------------------------------------------------------------

SEARCH_TERMS = [
    "pokemon 30 aniversario",
    "pokemon 30.º aniversario",
    "pokemon 30º aniversario",
    "pokemon 30o aniversario",
    "pokemon 30th anniversary",
    "pokemon 30th celebration",
    "pokemon tcg 30 aniversario",
    "pokemon tcg 30.º aniversario",
    "pokemon tcg 30th celebration",
    "pokemon 30 aniversario ingles",
    "pokemon 30 aniversario english",
    "pokemon 30 aniversario mini tin",
    "pokemon 30 aniversario booster bundle",
    "pokemon 30 aniversario binder collection",
    "pokemon 30 aniversario elite trainer box",
    "pokemon 30 aniversario etb",
    "pokemon 30 aniversario poster collection",
    "pokemon 30 aniversario 3 pack",
]


@dataclass(frozen=True)
class Store:
    name: str
    search_url_template: str


# URL placeholders:
# {query}      URL-encoded query, spaces encoded as %20
# {query_plus} URL-encoded query, spaces encoded as +
# {query_path} URL-encoded query intended for a URL path
STORES = [
    Store(
        "Plaza Vea",
        "https://www.plazavea.com.pe/search/?_query={query}",
    ),
    Store(
        "Saga Falabella",
        "https://www.falabella.com.pe/falabella-pe/search?Ntt={query}",
    ),
    Store(
        "Ripley",
        "https://simple.ripley.com.pe/search/{query_path}",
    ),
    Store(
        "Tai Loy",
        "https://www.tailoy.com.pe/catalogsearch/result/?q={query_plus}",
    ),
    Store(
        "Phantom",
        "https://phantom.pe/catalogsearch/result/?q={query_plus}",
    ),
    Store(
        "LawGamers",
        "https://lawgamers.com/?s={query_plus}&post_type=product",
    ),
    Store(
        "Oechsle",
        "https://www.oechsle.pe/search/?text={query}",
    ),
    Store(
        "Metro",
        "https://www.metro.pe/search?text={query}",
    ),
    Store(
        "Wong",
        "https://www.wong.pe/search?text={query}",
    ),
    Store(
        "Pharmax",
        "https://pharmax.com.pe/search?q={query}&type=product",
    ),
    Store(
        "Ilahui",
        (
            "https://ilahuiperu.com/search?"
            "options%5Bunavailable_products%5D=last&"
            "options%5Bprefix%5D=last&"
            "options%5Bfields%5D=title%2Cvendor%2Cproduct_type%2Cvariants.title&"
            "q={query_plus}"
        ),
    ),
]


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize(value: Any) -> str:
    text = clean_text(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return text.lower()


def canonical_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                "",
                "",
            )
        )
    except Exception:
        return url


def product_key(store_name: str, url: str) -> str:
    return f"{store_name}|{canonical_url(url)}"


def make_search_url(store: Store, search_term: str) -> str:
    return store.search_url_template.format(
        query=quote(search_term, safe=""),
        query_plus=quote_plus(search_term),
        query_path=quote(search_term, safe=""),
    )


def is_target_product(text: str) -> bool:
    """
    Requires:
      - Pokémon
      - 30th Anniversary / 30.º Anniversary / 30th Celebration
      - English / Inglés
    """
    text = normalize(text)

    has_pokemon = "pokemon" in text

    has_30th = bool(
        re.search(
            r"(?:"
            r"\b30\s*(?:\.?\s*[oº°])?\s*(?:th\s*)?"
            r"(?:aniversario|anniversary|celebration)\b"
            r"|"
            r"\b30th\s*(?:anniversary|celebration)\b"
            r")",
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
        "producto no disponible",
        "temporalmente no disponible",
        "out of stock",
        "sold out",
        "not available",
        "unavailable",
    ]

    return any(marker in text for marker in sold_out_markers)


def has_price(text: str) -> bool:
    return bool(
        re.search(
            r"(?:s\/|s\.\s*|pen\s*|precio\s*:?\s*)\s*\d+(?:[.,]\d{1,2})?",
            normalize(text),
        )
    )


def extract_price(text: str) -> str:
    text = clean_text(text)

    patterns = [
        r"(S\/\s*\d+(?:[.,]\d{1,2})?)",
        r"(S\.\s*\d+(?:[.,]\d{1,2})?)",
        r"(PEN\s*\d+(?:[.,]\d{1,2})?)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(1))

    return ""


def is_product_like_url(url: str) -> bool:
    """
    Identifies common product URL structures while avoiding category and
    search-result links. Individual product URLs are never hardcoded.
    """
    parsed = urlsplit(url)
    path = parsed.path.lower()

    product_path_markers = [
        "/products/",
        "/product/",
        "/producto/",
        "/productos/",
        "/p/",
        "/item/",
        "/sku/",
        "/catalog/product/",
        "/catalogo/",
        ".html",
    ]

    return any(marker in path for marker in product_path_markers)


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
        "security check",
    ]

    return any(marker in text for marker in blocked_markers)


def page_has_explicit_no_results(body_text: str) -> bool:
    text = normalize(body_text)

    no_results_markers = [
        "no se encontraron productos",
        "no encontramos productos",
        "no se encontraron resultados",
        "no se encontraron articulos",
        "sin resultados",
        "sin productos",
        "no results found",
        "your search did not match any products",
        "no products found",
    ]

    return any(marker in text for marker in no_results_markers)


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "version": 3,
            "created_at": now_iso(),
            "products": {},
        }

    try:
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as error:
        print(
            f"State file could not be read; starting with an empty state: {error}",
            file=sys.stderr,
        )
        return {
            "version": 3,
            "created_at": now_iso(),
            "products": {},
        }

    if not isinstance(loaded, dict):
        return {
            "version": 3,
            "created_at": now_iso(),
            "products": {},
        }

    if not isinstance(loaded.get("products"), dict):
        loaded["products"] = {}

    loaded["version"] = 3
    return loaded


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()

    temporary_file = STATE_FILE.with_suffix(f"{STATE_FILE.suffix}.tmp")

    temporary_file.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
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
    alert_type = clean_text(product.get("alert_type", "IN STOCK"))
    store = clean_text(product.get("store", "Unknown store"))
    title = clean_text(product.get("title", "Pokémon TCG listing"))
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
                    "text": (
                        "Catalog/search monitor — verify checkout availability "
                        "immediately"
                    )
                },
                "timestamp": now_iso(),
            }
        ],
    }

    delivered, detail = await asyncio.to_thread(discord_post, payload)

    if delivered:
        print(f"Discord alert delivered: {store} | {title}")
    else:
        print(f"Discord alert failed: {store} | {title} | {detail}")

    return delivered


# ---------------------------------------------------------------------------
# Playwright catalog extraction
# ---------------------------------------------------------------------------

ANCHOR_EXTRACTION_JS = r"""
anchors => anchors.slice(0, 5000).map(anchor => {
    const card =
        anchor.closest(
            "article, li, " +
            "[data-testid*='product'], " +
            "[class*='product-card'], " +
            "[class*='ProductCard'], " +
            "[class*='product-item'], " +
            "[class*='ProductItem'], " +
            "[class*='product__item'], " +
            "[class*='Product__Item']"
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
    "[class*='ProductItem'], "
    "[class*='product__item'], "
    "[class*='Product__Item']"
)


async def safely_scroll_for_lazy_products(page: Page) -> None:
    """
    Allows normal lazy-loaded catalog content to appear. It does not bypass
    access controls, CAPTCHAs, bot challenges, or other site protections.
    """
    for _ in range(2):
        await page.evaluate(
            "() => window.scrollTo(0, document.body.scrollHeight)"
        )
        await page.wait_for_timeout(SCROLL_WAIT_MS)

    await page.evaluate("() => window.scrollTo(0, 0)")
    await page.wait_for_timeout(250)


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


def product_title_from_candidate(candidate: dict[str, Any]) -> str:
    title = clean_text(
        candidate.get("title")
        or candidate.get("text")
        or candidate.get("ariaLabel")
        or ""
    )

    parent_text = clean_text(candidate.get("parentText", ""))

    if (
        parent_text
        and is_target_product(parent_text)
        and len(parent_text) <= 700
        and len(parent_text) > len(title)
    ):
        return parent_text

    return title or "Pokémon TCG 30th product"


def extract_target_products(
    raw_candidates: list[dict[str, Any]],
    store_name: str,
    search_term: str,
) -> list[dict[str, Any]]:
    products_by_url: dict[str, dict[str, Any]] = {}

    for candidate in raw_candidates:
        url = clean_text(candidate.get("href", ""))

        if not url.startswith(("https://", "http://")):
            continue

        if url.endswith("#"):
            continue

        context = candidate_context(candidate)

        if not is_target_product(context):
            continue

        # Require a product-like URL. This prevents navigation/category links
        # from being treated as products merely because parent text is broad.
        if not is_product_like_url(url):
            continue

        key = canonical_url(url)

        product = {
            "store": store_name,
            "url": url,
            "title": product_title_from_candidate(candidate)[:700],
            "price": extract_price(context),
            "available": not is_sold_out(context),
            "matched_search_terms": [search_term],
        }

        existing = products_by_url.get(key)

        if existing is None:
            products_by_url[key] = product
            continue

        if search_term not in existing["matched_search_terms"]:
            existing["matched_search_terms"].append(search_term)

        if len(product["title"]) > len(existing["title"]):
            existing["title"] = product["title"]

        if product["price"] and not existing["price"]:
            existing["price"] = product["price"]

        if product["available"]:
            existing["available"] = True

    return list(products_by_url.values())


async def scrape_single_search(
    context: BrowserContext,
    store: Store,
    search_term: str,
) -> dict[str, Any]:
    page = await context.new_page()
    search_url = make_search_url(store, search_term)

    try:
        response = await page.goto(
            search_url,
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

        explicit_no_results = page_has_explicit_no_results(body_text)

        if status >= 400:
            return {
                "verified": False,
                "blocked": status in {401, 403, 429},
                "reason": f"HTTP {status} returned by store",
                "status": status,
                "search_term": search_term,
                "search_url": search_url,
                "final_url": final_url,
                "page_title": page_title,
                "anchors": anchor_count,
                "cards": card_count,
                "product_links": product_link_count,
                "targets": [],
            }

        if page_looks_blocked(body_text, page_title):
            return {
                "verified": False,
                "blocked": True,
                "reason": (
                    "access/error page detected "
                    "(Cloudflare, CAPTCHA, denial, or temporary error)"
                ),
                "status": status,
                "search_term": search_term,
                "search_url": search_url,
                "final_url": final_url,
                "page_title": page_title,
                "anchors": anchor_count,
                "cards": card_count,
                "product_links": product_link_count,
                "targets": [],
            }

        # A page is only verified if it contains at least one recognizable
        # product link OR an explicit no-results message.
        #
        # Do not use len(raw_cards) here. Generic page-layout elements can
        # match card selectors without proving that a catalog was extracted.
        has_catalog_evidence = (
            product_link_count > 0
            or explicit_no_results
        )

        if not has_catalog_evidence:
            return {
                "verified": False,
                "blocked": False,
                "reason": (
                    "page loaded but contained no recognizable product-link "
                    "evidence or explicit no-results message"
                ),
                "status": status,
                "search_term": search_term,
                "search_url": search_url,
                "final_url": final_url,
                "page_title": page_title,
                "anchors": anchor_count,
                "cards": card_count,
                "product_links": product_link_count,
                "targets": [],
            }

        target_products = extract_target_products(
            raw_candidates,
            store.name,
            search_term,
        )

        return {
            "verified": True,
            "blocked": False,
            "reason": (
                "explicit no-results page"
                if explicit_no_results
                else "catalog product-link evidence found"
            ),
            "status": status,
            "search_term": search_term,
            "search_url": search_url,
            "final_url": final_url,
            "page_title": page_title,
            "anchors": anchor_count,
            "cards": card_count,
            "product_links": product_link_count,
            "targets": target_products,
        }

    except Exception as error:
        return {
            "verified": False,
            "blocked": False,
            "reason": f"scrape exception: {type(error).__name__}: {error}",
            "status": 0,
            "search_term": search_term,
            "search_url": search_url,
            "final_url": page.url,
            "page_title": "",
            "anchors": 0,
            "cards": 0,
            "product_links": 0,
            "targets": [],
        }

    finally:
        await page.close()


async def scrape_store(
    context: BrowserContext,
    store: Store,
) -> dict[str, Any]:
    """
    Runs every search term for one store and merges all matching products.

    A store is considered fully verified only if every requested search term
    produced catalog evidence or an explicit no-results response.

    If even one term is blocked, times out, or lacks catalog evidence,
    the monitor preserves missing-product state for that store. New available
    listings found in successful searches may still be alerted.
    """
    query_results: list[dict[str, Any]] = []
    merged_targets: dict[str, dict[str, Any]] = {}

    for index, search_term in enumerate(SEARCH_TERMS, start=1):
        result = await scrape_single_search(context, store, search_term)
        query_results.append(result)

        for product in result["targets"]:
            key = canonical_url(product["url"])

            if key not in merged_targets:
                merged_targets[key] = product
                continue

            existing = merged_targets[key]

            for matched_term in product["matched_search_terms"]:
                if matched_term not in existing["matched_search_terms"]:
                    existing["matched_search_terms"].append(matched_term)

            if len(product["title"]) > len(existing["title"]):
                existing["title"] = product["title"]

            if product["price"] and not existing["price"]:
                existing["price"] = product["price"]

            if product["available"]:
                existing["available"] = True

        # Stop additional queries if the store is clearly refusing access.
        # This avoids repeatedly hitting a blocked site during the same cycle.
        if result["blocked"]:
            break

        if index < len(SEARCH_TERMS):
            await asyncio.sleep(QUERY_DELAY_MS / 1000)

    successful_queries = [
        result for result in query_results if result["verified"]
    ]

    failed_queries = [
        result for result in query_results if not result["verified"]
    ]

    all_requested_queries_completed = len(query_results) == len(SEARCH_TERMS)

    fully_verified = (
        all_requested_queries_completed
        and len(successful_queries) == len(SEARCH_TERMS)
        and not failed_queries
    )

    return {
        "store": store,
        "fully_verified": fully_verified,
        "has_verified_catalog_data": bool(successful_queries),
        "queries_attempted": len(query_results),
        "queries_requested": len(SEARCH_TERMS),
        "queries_verified": len(successful_queries),
        "queries_failed": len(failed_queries),
        "targets": list(merged_targets.values()),
        "query_results": query_results,
    }


# ---------------------------------------------------------------------------
# Verification logs
# ---------------------------------------------------------------------------

def print_store_verification(result: dict[str, Any]) -> None:
    store: Store = result["store"]

    if result["fully_verified"]:
        verification = "VERIFY PASS"
        state_action = "state may be updated"
    elif result["has_verified_catalog_data"]:
        verification = "VERIFY PARTIAL"
        state_action = "missing-product state preserved"
    else:
        verification = "VERIFY PRESERVE STATE"
        state_action = "state preserved"

    total_anchors = sum(
        item["anchors"] for item in result["query_results"]
    )

    total_cards = sum(
        item["cards"] for item in result["query_results"]
    )

    total_product_links = sum(
        item["product_links"] for item in result["query_results"]
    )

    print(
        f"{store.name}: [{verification}] "
        f"queries={result['queries_verified']}/{result['queries_requested']} "
        f"verified | attempted={result['queries_attempted']} | "
        f"failed={result['queries_failed']} | "
        f"anchors={total_anchors} | cards={total_cards} | "
        f"product-links={total_product_links} | "
        f"matching-targets={len(result['targets'])} | "
        f"{state_action}"
    )

    for item in result["query_results"]:
        status = "PASS" if item["verified"] else "PRESERVE"

        print(
            f"  [{status}] term={item['search_term']!r} | "
            f"HTTP {item['status']} | "
            f"anchors={item['anchors']} | "
            f"cards={item['cards']} | "
            f"product-links={item['product_links']} | "
            f"targets={len(item['targets'])} | "
            f"reason={item['reason']} | "
            f"final-url={item['final_url']}"
        )

    for listing in result["targets"]:
        availability = "IN STOCK" if listing["available"] else "SOLD OUT"
        search_terms = ", ".join(listing["matched_search_terms"])

        print(
            f"  MATCH [{availability}] {listing['title']} | "
            f"{listing['price'] or 'price not found'} | "
            f"terms: {search_terms} | {listing['url']}"
        )


# ---------------------------------------------------------------------------
# State and restock processing
# ---------------------------------------------------------------------------

def update_state_from_result(
    state: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """
    Safe state rules:

    1. A product found in a verified search can be created/updated immediately.
    2. A previously known product is marked unavailable for being absent only
       after every configured search term completed with verified catalog data.
    3. Partial, blocked, timed-out, redirected, or invalid catalog responses
       never mark existing products unavailable.
    """
    products_state: dict[str, Any] = state["products"]
    store: Store = result["store"]

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
                "matched_search_terms": listing["matched_search_terms"],
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
        old["available"] = available
        old["last_seen"] = now_iso()
        old["matched_search_terms"] = listing["matched_search_terms"]

        if available and not was_available:
            old["last_status_change"] = now_iso()
            old["pending_alert"] = True
            old["alert_type"] = "RESTOCK"

        elif not available and was_available:
            old["last_status_change"] = now_iso()
            old["pending_alert"] = False
            old["alert_type"] = ""

    # Only a complete, fully verified store scan is allowed to interpret a
    # missing prior product as unavailable.
    if not result["fully_verified"]:
        return

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

    for product in products_state.values():
        if not product.get("pending_alert", False):
            continue

        if not product.get("available", False):
            product["pending_alert"] = False
            continue

        delivered = await send_discord_alert(product)

        if delivered:
            product["pending_alert"] = False
            product["last_alert"] = now_iso()


# ---------------------------------------------------------------------------
# Main monitoring loop
# ---------------------------------------------------------------------------

async def run_scan_cycle(cycle_number: int) -> None:
    print(
        f"\n=== Starting scan cycle #{cycle_number} "
        f"[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] ==="
    )

    if VERIFY_ONLY:
        print(
            "VERIFY_ONLY=1: no state will be changed and no Discord alerts "
            "will be sent."
        )

    state = load_state()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=HEADLESS)

        context = await browser.new_context(
            viewport={"width": 1440, "height": 1100},
            locale="es-PE",
            timezone_id="America/Lima",
        )

        try:
            for store in STORES:
                result = await scrape_store(context, store)
                print_store_verification(result)

                if not VERIFY_ONLY:
                    update_state_from_result(state, result)

        finally:
            await context.close()
            await browser.close()

    if not VERIFY_ONLY:
        await dispatch_pending_alerts(state)
        save_state(state)

    print("=== Cycle complete. ===")


async def main() -> None:
    print("Pokémon English 30th Anniversary / Celebration Catalog Monitor")
    print(f"Configured stores: {len(STORES)}")
    print(f"Search terms per store: {len(SEARCH_TERMS)}")
    print(f"State file: {STATE_FILE.name}")
    print(f"Minimum requested scan interval: {SCAN_SECONDS} seconds")
    print(f"Run once: {RUN_ONCE}")
    print(f"Verification only: {VERIFY_ONLY}")

    if not VERIFY_ONLY and not DISCORD_WEBHOOK_URL:
        print(
            "WARNING: DISCORD_WEBHOOK_URL is missing. The monitor can scan, "
            "but Discord alerts cannot be delivered.",
            file=sys.stderr,
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
        print(f"Next scan begins in {SCAN_SECONDS} seconds.")
        await asyncio.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
