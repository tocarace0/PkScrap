import asyncio
import json
import os
import random
import re
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.async_api import async_playwright


# ============================================================
# CONFIGURATION
# ============================================================

DISCORD_WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"

# This file keeps the previous availability state of every discovered product.
# It is required to detect a product disappearing and later returning as stock.
PRODUCT_STATE_FILE = "productos_estado_english_catalog.json"

# Main timeout for each store search/catalog page.
PAGE_TIMEOUT_MS = 20000

# Wait for product cards rendered by JavaScript after the page DOM is ready.
MIN_WAIT_AFTER_PAGE_LOAD_MS = 1200
MAX_WAIT_AFTER_PAGE_LOAD_MS = 2200

# Full scan interval. Keep it reasonable to avoid overloading store sites.
MIN_WAIT_BETWEEN_CYCLES_SECONDS = 60
MAX_WAIT_BETWEEN_CYCLES_SECONDS = 90

# Avoid collecting too many duplicate/malformed product links per store.
MAX_PRODUCTS_PER_STORE = 60

# Maximum saved products in local state.
MAX_SAVED_PRODUCT_STATES = 5000

# A product must be absent this many consecutive successful catalog scans
# before being marked out of stock. Set to 1 for the fastest restock tracking.
MISSING_SCANS_TO_MARK_OUT_OF_STOCK = 1

# True: alerts for all English Pokémon 30th Anniversary / Celebration products.
# This includes:
# - Mini Tin
# - Booster Bundle
# - Binder Collection
# - Elite Trainer Box / ETB
# - Poster Collection
# - 3 Pack products, including TCG FACTORY listings
#
# False: alerts only for Mini Tin, Booster Bundle, Binder Collection, and ETB.
MONITOR_ALL_ENGLISH_30TH_PRODUCTS = True


# ============================================================
# STORE SEARCH CONFIGURATION
# ============================================================

TIENDAS = {
    "Plaza Vea": {
        "url": "https://www.plazavea.com.pe/search?q=pokemon+tcg",
        "base": "https://www.plazavea.com.pe",
    },
    "Saga Falabella": {
        "url": (
            "https://www.falabella.com.pe/"
            "falabella-pe/search?Ntt=pokemon+30"
        ),
        "base": "https://www.falabella.com.pe",
    },
    "Ripley": {
        "url": "https://simple.ripley.com.pe/search/pokemon+tcg",
        "base": "https://simple.ripley.com.pe",
    },
    "Tai Loy": {
        "url": (
            "https://www.tailoy.com.pe/"
            "catalogsearch/result/?q=pokemon+tcg"
        ),
        "base": "https://www.tailoy.com.pe",
    },
    "Phantom": {
        "url": (
            "https://www.phantom.pe/"
            "catalogsearch/result/?q=pokemon+tcg"
        ),
        "base": "https://www.phantom.pe",
    },
    "LawGamers": {
        "url": (
            "https://www.lawgamers.com/"
            "?s=pokemon+tcg&post_type=product"
        ),
        "base": "https://www.lawgamers.com",
    },
    "Oechsle": {
        "url": "https://www.oechsle.pe/search?q=pokemon+tcg",
        "base": "https://www.oechsle.pe",
    },
    "Metro": {
        "url": "https://www.metro.pe/search?q=pokemon+tcg",
        "base": "https://www.metro.pe",
    },
    "Wong": {
        "url": "https://www.wong.pe/search?q=pokemon+tcg",
        "base": "https://www.wong.pe",
    },
}


# ============================================================
# OPTIONAL PRODUCT-FAMILY FILTER
# ============================================================

TARGET_PRODUCT_PATTERNS = (
    r"\bmini\s*tin\b",
    r"\bbooster\s*bundle\b",
    r"\bbinder\s*collection\b",
    r"\belite\s*trainer\s*box\b",
    r"\betb\b",
)


# ============================================================
# TEXT AND URL HELPERS
# ============================================================

def normalizar_texto(texto):
    """Converts text to lowercase and removes accents."""
    texto_normalizado = unicodedata.normalize("NFD", texto.lower())

    return "".join(
        caracter
        for caracter in texto_normalizado
        if unicodedata.category(caracter) != "Mn"
    )


def texto_error_corto(error, limite=200):
    """Returns a compact single-line error message."""
    texto = str(error).replace("\n", " ").strip()

    if len(texto) > limite:
        return texto[:limite] + "..."

    return texto or "Unknown error"


def fecha_utc_actual():
    """Returns the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def canonicalizar_link(link):
    """
    Removes query strings and fragments so product tracking parameters do not
    create separate stock records for the same product.
    """
    partes = urlsplit(link)

    return urlunsplit((
        partes.scheme,
        partes.netloc,
        partes.path.rstrip("/"),
        "",
        "",
    ))


def es_producto_ingles(texto):
    """Requires an explicit English/English-language marker."""
    texto_normalizado = normalizar_texto(texto)

    return any(
        indicador in texto_normalizado
        for indicador in (
            "english",
            "ingles",
        )
    )


def es_producto_30_aniversario_o_celebration(texto):
    """
    Accepts common product naming variants:

    - 30.º Aniversario
    - 30° Aniversario
    - 30 Aniversario
    - 30th Anniversary
    - 30th Celebration
    - 30th Celebrations
    - Celebration 30
    - Aniversario 30
    - Colección 30
    """
    texto_normalizado = normalizar_texto(texto)

    patrones_validos = (
        r"\b30\s*(?:th|[.\u00ba\u00b0o]+)?\s*aniversario\b",
        r"\b30\s*(?:th|[.\u00ba\u00b0o]+)?\s*anniversary\b",
        r"\b30\s*(?:th|[.\u00ba\u00b0o]+)?\s*celebration(?:s)?\b",
        r"\b30th\s+anniversary\b",
        r"\b30th\s+celebration(?:s)?\b",
        r"\baniversario\s*(?:[.\u00ba\u00b0o]+)?\s*30\b",
        r"\banniversary\s*(?:[.\u00ba\u00b0o]+)?\s*30\b",
        r"\bcelebration(?:s)?\s*(?:[.\u00ba\u00b0o]+)?\s*30\b",
        r"\bcelebracion(?:es)?\s*(?:[.\u00ba\u00b0o]+)?\s*30\b",
        r"\bcoleccion\s*(?:[.\u00ba\u00b0o]+)?\s*30\b",
        r"\bcollection\s*(?:[.\u00ba\u00b0o]+)?\s*30\b",
    )

    patrones_falsos = (
        r"\b20th\b",
        r"\b25th\b",
        r"\b20\s*aniversario\b",
        r"\b25\s*aniversario\b",
        r"\b20\s*anniversary\b",
        r"\b25\s*anniversary\b",
        r"\b30\s*cm\b",
    )

    if any(
        re.search(patron, texto_normalizado)
        for patron in patrones_falsos
    ):
        return False

    return any(
        re.search(patron, texto_normalizado)
        for patron in patrones_validos
    )


def pertenece_a_familia_objetivo(texto):
    """Checks Mini Tin, Booster Bundle, Binder Collection, ETB/Elite Trainer."""
    texto_normalizado = normalizar_texto(texto)

    return any(
        re.search(patron, texto_normalizado)
        for patron in TARGET_PRODUCT_PATTERNS
    )


def es_producto_objetivo(texto):
    """
    Main product filter.

    Default behavior monitors every English Pokémon 30th product.
    If MONITOR_ALL_ENGLISH_30TH_PRODUCTS is False, only the requested
    product families are monitored.
    """
    texto_normalizado = normalizar_texto(texto)

    if "pokemon" not in texto_normalizado:
        return False

    if not es_producto_ingles(texto_normalizado):
        return False

    if not es_producto_30_aniversario_o_celebration(texto_normalizado):
        return False

    if MONITOR_ALL_ENGLISH_30TH_PRODUCTS:
        return True

    return pertenece_a_familia_objetivo(texto_normalizado)


def obtener_titulo(texto_ancla, texto_tarjeta):
    """
    Chooses the best product title from anchor text and its parent card text.
    """
    lineas = []

    for origen in (texto_ancla, texto_tarjeta):
        lineas.extend(
            linea.strip()
            for linea in origen.split("\n")
            if linea.strip()
        )

    for linea in lineas:
        if len(linea) > 350:
            continue

        if es_producto_objetivo(linea):
            return linea

    for linea in lineas:
        texto_normalizado = normalizar_texto(linea)

        if (
            "pokemon" in texto_normalizado
            and len(linea) >= 10
            and len(linea) <= 350
        ):
            return linea

    for linea in lineas:
        if (
            len(linea) >= 10
            and len(linea) <= 350
            and "s/" not in linea.lower()
        ):
            return linea

    return "Pokémon TCG product"


def obtener_precio(texto):
    """Extracts the first Peruvian Sol price found in scraped card text."""
    coincidencia = re.search(
        r"(?:S/|s/)\s*[\d.,]+",
        texto,
    )

    if coincidencia:
        return coincidencia.group(0)

    return "Precio no disponible"


# ============================================================
# PRODUCT STATE STORAGE
# ============================================================

def cargar_estados_productos():
    """Loads previously observed product stock states."""
    try:
        if not os.path.exists(PRODUCT_STATE_FILE):
            return {}

        with open(
            PRODUCT_STATE_FILE,
            "r",
            encoding="utf-8",
        ) as archivo:
            datos = json.load(archivo)

        if isinstance(datos, dict):
            return datos

    except (OSError, json.JSONDecodeError) as error:
        print(
            f"[WARNING] Could not read {PRODUCT_STATE_FILE}: {error}"
        )

    return {}


def guardar_estados_productos(estados):
    """Saves product states atomically."""
    try:
        if len(estados) > MAX_SAVED_PRODUCT_STATES:
            claves = sorted(
                estados,
                key=lambda clave: estados[clave].get(
                    "last_seen_utc",
                    "",
                ),
            )

            exceso = len(estados) - MAX_SAVED_PRODUCT_STATES

            for clave in claves[:exceso]:
                estados.pop(clave, None)

        archivo_temporal = PRODUCT_STATE_FILE + ".tmp"

        with open(
            archivo_temporal,
            "w",
            encoding="utf-8",
        ) as archivo:
            json.dump(
                estados,
                archivo,
                ensure_ascii=False,
                indent=2,
            )

        os.replace(archivo_temporal, PRODUCT_STATE_FILE)

    except OSError as error:
        print(
            f"[WARNING] Could not save {PRODUCT_STATE_FILE}: {error}"
        )


def clasificar_evento_stock(estados, producto):
    """
    Returns NEW STOCK or RESTOCK only when the catalog currently contains
    the matching listing.
    """
    anterior = estados.get(producto["state_key"])

    if anterior is None:
        return "NEW STOCK"

    if anterior.get("in_stock") is False:
        return "RESTOCK"

    return None


def registrar_producto_disponible(estados, producto):
    """Records a product that is currently visible in a successful catalog."""
    clave = producto["state_key"]
    anterior = estados.get(clave, {})
    ahora = fecha_utc_actual()

    estados[clave] = {
        "tienda": producto["tienda"],
        "titulo": producto["titulo"],
        "precio": producto["precio"],
        "link": producto["link"],
        "in_stock": True,
        "missing_scans": 0,
        "last_seen_utc": ahora,
        "last_stock_change_utc": anterior.get(
            "last_stock_change_utc",
            ahora,
        ),
    }


def registrar_producto_agotado(estados, producto):
    """Records an explicit out-of-stock label from the product card."""
    clave = producto["state_key"]
    anterior = estados.get(clave, {})
    ahora = fecha_utc_actual()

    estados[clave] = {
        "tienda": producto["tienda"],
        "titulo": producto["titulo"],
        "precio": producto["precio"],
        "link": producto["link"],
        "in_stock": False,
        "missing_scans": 0,
        "last_seen_utc": ahora,
        "last_stock_change_utc": (
            ahora
            if anterior.get("in_stock") is not False
            else anterior.get("last_stock_change_utc", ahora)
        ),
    }


def marcar_faltantes_como_agotados(estados, tienda, links_vistos):
    """
    Mark a previously known in-stock product as unavailable only after a
    successful and usable catalog scrape where its link is absent.

    This is never called after HTTP errors, Cloudflare pages, 403/524, or
    invalid/error pages.
    """
    cambios = 0
    ahora = fecha_utc_actual()

    for clave, estado in estados.items():
        if estado.get("tienda") != tienda:
            continue

        if estado.get("in_stock") is not True:
            continue

        if clave in links_vistos:
            continue

        faltantes = int(estado.get("missing_scans", 0)) + 1
        estado["missing_scans"] = faltantes

        if faltantes >= MISSING_SCANS_TO_MARK_OUT_OF_STOCK:
            estado["in_stock"] = False
            estado["last_stock_change_utc"] = ahora

            print(
                f"{tienda}: Marked OUT OF STOCK after "
                f"{faltantes} missing successful catalog scan(s): "
                f"{estado.get('titulo', clave)}"
            )

        cambios += 1

    return cambios


# ============================================================
# DISCORD WEBHOOK
# ============================================================

async def enviar_discord(webhook_url, tienda, eventos):
    """Sends NEW STOCK and RESTOCK notifications to Discord."""
    bloques = []

    for evento in eventos:
        producto = evento["producto"]

        bloques.append(
            f"**{evento['tipo_evento']} — {producto['titulo']}**\n"
            f"Price: {producto['precio']}\n"
            f"Store: {producto['tienda']}\n"
            f"Language: English\n"
            f"Detection: {producto['stock_source']}\n"
            f"Link: {producto['link']}"
        )

    encabezado = (
        f"**POKÉMON TCG 30TH ENGLISH ALERT — {tienda}**\n"
        f"Events: {len(eventos)}\n\n"
    )

    mensajes = []
    mensaje_actual = encabezado

    for bloque in bloques:
        bloque_con_salto = bloque + "\n\n"

        if len(mensaje_actual) + len(bloque_con_salto) > 1900:
            mensajes.append(mensaje_actual)
            mensaje_actual = encabezado + bloque_con_salto
        else:
            mensaje_actual += bloque_con_salto

    if mensaje_actual.strip():
        mensajes.append(mensaje_actual)

    def publicar(payload):
        datos = json.dumps(payload).encode("utf-8")

        solicitud = urllib.request.Request(
            webhook_url,
            data=datos,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "PKScrapCatalogMonitor/2.0",
            },
            method="POST",
        )

        with urllib.request.urlopen(
            solicitud,
            timeout=20,
        ) as respuesta:
            return respuesta.status

    for contenido in mensajes:
        payload = {
            "username": "Pokemon English Stock Alert",
            "content": contenido[:1900],
            "allowed_mentions": {
                "parse": [],
            },
        }

        try:
            estado = await asyncio.to_thread(publicar, payload)

            if estado not in (200, 204):
                print(
                    f"[ERROR] Discord returned unexpected HTTP status "
                    f"{estado}"
                )
                return False

        except urllib.error.HTTPError as error:
            print(
                f"[ERROR] Discord HTTP {error.code}: "
                f"{texto_error_corto(error)}"
            )
            return False

        except Exception as error:
            print(
                f"[ERROR] Discord notification failed: "
                f"{texto_error_corto(error)}"
            )
            return False

    return True


# ============================================================
# SEARCH PAGE SCRAPING
# ============================================================

def parece_pagina_error(titulo, texto):
    """
    Detects known error/access pages.

    It intentionally does not classify every Cloudflare-enabled normal page
    as an error; it looks for actual challenge/error wording.
    """
    contenido = normalizar_texto(
        f"{titulo}\n{texto[:5000]}"
    )

    indicadores = (
        "error code 524",
        "a timeout occurred",
        "error code 520",
        "error code 521",
        "error code 522",
        "error code 523",
        "error code 525",
        "error code 526",
        "access denied",
        "forbidden",
        "checking your browser",
        "just a moment",
        "attention required",
        "temporarily unavailable",
        "service unavailable",
    )

    return any(
        indicador in contenido
        for indicador in indicadores
    )


async def scrapear_catalogo_tienda(page, tienda, datos):
    """
    Scrapes product listings from the live store search/catalog page.

    This does not use fixed individual product URLs. Product links are
    discovered from the store's search page every cycle.

    Unlike the recent restrictive version, this function does not immediately
    abort merely because a response status is 403. It inspects the rendered
    content first. If the page only contains a denial/error screen, it is
    treated as unavailable and product state is preserved.
    """
    respuesta = await page.goto(
        datos["url"],
        timeout=PAGE_TIMEOUT_MS,
        wait_until="domcontentloaded",
    )

    http_status = respuesta.status if respuesta is not None else None

    await page.wait_for_timeout(
        random.randint(
            MIN_WAIT_AFTER_PAGE_LOAD_MS,
            MAX_WAIT_AFTER_PAGE_LOAD_MS,
        )
    )

    snapshot = await page.evaluate(
        """
        () => {
            const clean = (value) =>
                String(value || "")
                    .replace(/\\s+/g, " ")
                    .trim();

            const visible = (element) => Boolean(
                element.offsetWidth ||
                element.offsetHeight ||
                element.getClientRects().length
            );

            const findCard = (anchor) => {
                let current = anchor;
                let best = {
                    node: anchor,
                    text: clean(anchor.innerText),
                    score: 0
                };

                for (let level = 0; level < 8; level += 1) {
                    current = current.parentElement;

                    if (!current) {
                        break;
                    }

                    const text = clean(current.innerText);
                    const className = clean(current.className).toLowerCase();

                    if (
                        text.length < 8 ||
                        text.length > 3000
                    ) {
                        continue;
                    }

                    let score = 0;

                    if (
                        current.tagName === "ARTICLE" ||
                        current.tagName === "LI"
                    ) {
                        score += 20;
                    }

                    if (
                        className.includes("product") ||
                        className.includes("card") ||
                        className.includes("item") ||
                        className.includes("result")
                    ) {
                        score += 15;
                    }

                    if (/s\\/\\s*[\\d,.]+/i.test(text)) {
                        score += 8;
                    }

                    if (
                        /agregar al carrito|add to cart|agotado|sin stock|out of stock|disponible/i.test(text)
                    ) {
                        score += 8;
                    }

                    score -= level;

                    if (score > best.score) {
                        best = {
                            node: current,
                            text,
                            score
                        };
                    }
                }

                return best;
            };

            const actionSelector = [
                "button",
                "input[type='button']",
                "input[type='submit']",
                "[role='button']"
            ].join(",");

            const anchors = Array.from(
                document.querySelectorAll("a[href]")
            );

            const listings = [];

            for (const anchor of anchors) {
                const href = anchor.getAttribute("href") || "";
                const anchorText = clean(anchor.innerText);

                if (
                    !href ||
                    anchorText.length < 4 ||
                    anchorText.length > 600
                ) {
                    continue;
                }

                const card = findCard(anchor);

                const buttons = Array.from(
                    card.node.querySelectorAll(actionSelector)
                ).map((button) => ({
                    text: clean(
                        button.innerText ||
                        button.value ||
                        button.getAttribute("aria-label") ||
                        button.textContent
                    ),
                    disabled: Boolean(
                        button.disabled ||
                        button.getAttribute("aria-disabled") === "true" ||
                        button.classList.contains("disabled")
                    ),
                    visible: visible(button)
                })).filter((button) => button.text);

                listings.push({
                    href,
                    anchorText,
                    cardText: card.text,
                    buttons
                });
            }

            return {
                title: document.title || "",
                bodyText: document.body
                    ? clean(document.body.innerText)
                    : "",
                anchorCount: anchors.length,
                listings
            };
        }
        """
    )

    if parece_pagina_error(
        snapshot.get("title", ""),
        snapshot.get("bodyText", ""),
    ):
        raise RuntimeError(
            f"HTTP {http_status or 'unknown'} returned an access/error page"
        )

    productos = []
    links_vistos = set()
    near_matches = []

    for listing in snapshot.get("listings", []):
        href = listing.get("href", "")
        texto_ancla = listing.get("anchorText", "")
        texto_tarjeta = listing.get("cardText", "")

        if href.startswith((
            "#",
            "javascript:",
            "mailto:",
            "tel:",
        )):
            continue

        texto_completo = f"{texto_ancla}\n{texto_tarjeta}"
        texto_normalizado = normalizar_texto(texto_completo)

        # Keep near-match names for diagnostics if the store returned products
        # but title wording differs from the configured filters.
        if (
            "pokemon" in texto_normalizado
            and (
                "30" in texto_normalizado
                or "celebration" in texto_normalizado
                or "aniversario" in texto_normalizado
            )
            and len(near_matches) < 8
        ):
            near_matches.append(
                obtener_titulo(texto_ancla, texto_tarjeta)
            )

        if not es_producto_objetivo(texto_completo):
            continue

        link = urljoin(datos["base"], href)
        state_key = canonicalizar_link(link)

        if not state_key or state_key in links_vistos:
            continue

        links_vistos.add(state_key)

        titulo = obtener_titulo(
            texto_ancla,
            texto_tarjeta,
        )

        precio = obtener_precio(texto_tarjeta)

        in_stock, stock_source = detectar_estado_stock_tarjeta(
            texto_tarjeta,
            listing.get("buttons", []),
        )

        productos.append({
            "tienda": tienda,
            "titulo": titulo,
            "precio": precio,
            "link": link,
            "state_key": state_key,
            "in_stock": in_stock,
            "stock_source": stock_source,
        })

        if len(productos) >= MAX_PRODUCTS_PER_STORE:
            break

    catalogo_usable = (
        snapshot.get("anchorCount", 0) >= 5
        and len(snapshot.get("bodyText", "")) >= 100
    )

    return {
        "http_status": http_status,
        "catalogo_usable": catalogo_usable,
        "productos": productos,
        "near_matches": near_matches,
        "anchor_count": snapshot.get("anchorCount", 0),
        "page_title": snapshot.get("title", ""),
    }


def detectar_estado_stock_tarjeta(texto_tarjeta, botones):
    """
    Determines availability from catalog/search-card data.

    A visible matching catalog listing is treated as available when no
    explicit out-of-stock indication is present. This restores the behavior
    of the original working monitor versions.
    """
    texto = normalizar_texto(texto_tarjeta)

    patrones_carrito = (
        r"\bagregar al carrito\b",
        r"\banadir al carrito\b",
        r"\bcomprar ahora\b",
        r"\badd to cart\b",
        r"\bbuy now\b",
    )

    patrones_no_stock = (
        r"\bagotado\b",
        r"\bsin stock\b",
        r"\bout of stock\b",
        r"\bno disponible\b",
        r"\bproducto no disponible\b",
        r"\bnot available\b",
    )

    patrones_stock = (
        r"\ben stock\b",
        r"\bdisponible\b",
        r"\bultimas unidades\b",
        r"\bultima unidad\b",
        r"\b\d+\s*(?:unid|unidades|unidad)\b",
    )

    botones_carrito = []

    for boton in botones:
        if not boton.get("visible"):
            continue

        texto_boton = normalizar_texto(
            boton.get("text", "")
        )

        if any(
            re.search(patron, texto_boton)
            for patron in patrones_carrito
        ):
            botones_carrito.append(boton)

    if any(
        not boton.get("disabled")
        for boton in botones_carrito
    ):
        return True, "enabled add-to-cart in catalog"

    if botones_carrito and all(
        boton.get("disabled")
        for boton in botones_carrito
    ):
        return False, "disabled add-to-cart in catalog"

    if any(
        re.search(patron, texto)
        for patron in patrones_no_stock
    ):
        return False, "explicit out-of-stock label in catalog"

    if any(
        re.search(patron, texto)
        for patron in patrones_stock
    ):
        return True, "explicit stock label in catalog"

    return True, "matching live catalog listing"


# ============================================================
# SCAN CYCLE
# ============================================================

async def ejecutar_ciclo(browser, estados, webhook_url):
    """
    Scrapes every store catalog/search page, records stock state, and sends
    Discord messages only for NEW STOCK and RESTOCK events.
    """
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={
            "width": 1366,
            "height": 768,
        },
    )

    page = await context.new_page()

    try:
        for tienda, datos in TIENDAS.items():
            try:
                resultado = await scrapear_catalogo_tienda(
                    page,
                    tienda,
                    datos,
                )

            except Exception as error:
                print(
                    f"{tienda}: Catalog scrape unavailable; "
                    f"state preserved: {texto_error_corto(error)}"
                )
                continue

            productos = resultado["productos"]

            print(
                f"{tienda}: Catalog response HTTP "
                f"{resultado['http_status'] or 'unknown'} | "
                f"{resultado['anchor_count']} links | "
                f"{len(productos)} matching English 30th product(s)."
            )

            if not productos and resultado["near_matches"]:
                print(
                    f"{tienda}: Near-match diagnostics: "
                    f"{' | '.join(resultado['near_matches'][:5])}"
                )

            # Do not mark prior products unavailable if the store gave a page
            # too empty to be treated as a meaningful searchable catalog.
            if not resultado["catalogo_usable"]:
                print(
                    f"{tienda}: Catalog content was too limited to "
                    "update stock state; state preserved."
                )
                continue

            links_vistos = {
                producto["state_key"]
                for producto in productos
            }

            eventos_pendientes = []
            estado_cambio = False

            for producto in productos:
                if producto["in_stock"] is False:
                    registrar_producto_agotado(
                        estados,
                        producto,
                    )

                    estado_cambio = True

                    print(
                        f"{tienda}: Explicitly OUT OF STOCK: "
                        f"{producto['titulo']}"
                    )

                    continue

                tipo_evento = clasificar_evento_stock(
                    estados,
                    producto,
                )

                if tipo_evento:
                    eventos_pendientes.append({
                        "tipo_evento": tipo_evento,
                        "producto": producto,
                    })

                    print(
                        f"{tienda}: {tipo_evento}: "
                        f"{producto['titulo']} "
                        f"[{producto['stock_source']}]"
                    )
                else:
                    registrar_producto_disponible(
                        estados,
                        producto,
                    )

                    estado_cambio = True

                    print(
                        f"{tienda}: Still available; no repeat alert: "
                        f"{producto['titulo']}"
                    )

            cambios_faltantes = marcar_faltantes_como_agotados(
                estados,
                tienda,
                links_vistos,
            )

            if cambios_faltantes > 0:
                estado_cambio = True

            if estado_cambio:
                guardar_estados_productos(estados)

            if not eventos_pendientes:
                continue

            enviado = await enviar_discord(
                webhook_url,
                tienda,
                eventos_pendientes,
            )

            if enviado:
                for evento in eventos_pendientes:
                    registrar_producto_disponible(
                        estados,
                        evento["producto"],
                    )

                guardar_estados_productos(estados)

                print(
                    f"{tienda}: Discord sent "
                    f"{len(eventos_pendientes)} alert(s)."
                )
            else:
                print(
                    f"{tienda}: Discord failed; matching products "
                    "remain eligible for retry next cycle."
                )

    finally:
        await context.close()


# ============================================================
# MAIN LOOP
# ============================================================

async def main():
    webhook_url = os.getenv(DISCORD_WEBHOOK_ENV_VAR)

    if not webhook_url:
        print(
            f"[ERROR] {DISCORD_WEBHOOK_ENV_VAR} is not configured."
        )
        return

    estados = cargar_estados_productos()

    print("=" * 74)
    print("PKSCRAP CATALOG SCRAPER — ENGLISH POKÉMON 30TH MONITOR")
    print(f"Configured stores: {len(TIENDAS)}")
    print(f"Saved product states: {len(estados)}")
    print(
        "Scope: "
        + (
            "all English Pokémon 30th products"
            if MONITOR_ALL_ENGLISH_30TH_PRODUCTS
            else "Mini Tin, Booster Bundle, Binder Collection, and ETB only"
        )
    )
    print("Mode: search/catalog scraping only; no hardcoded product URLs.")
    print("Alerts: NEW STOCK and RESTOCK.")
    print("=" * 74)

    ciclo = 1

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
        )

        try:
            while True:
                fecha_hora = datetime.now().strftime(
                    "%d/%m/%Y %H:%M:%S"
                )

                print()
                print(
                    f"=== Starting scan cycle #{ciclo} "
                    f"[{fecha_hora}] ==="
                )

                await ejecutar_ciclo(
                    browser,
                    estados,
                    webhook_url,
                )

                espera = random.randint(
                    MIN_WAIT_BETWEEN_CYCLES_SECONDS,
                    MAX_WAIT_BETWEEN_CYCLES_SECONDS,
                )

                print(
                    f"=== Cycle complete. Next scan in "
                    f"{espera} seconds. ==="
                )

                await asyncio.sleep(espera)
                ciclo += 1

        finally:
            await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMonitor stopped by user.")
