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

# Saved product availability state for NEW STOCK and RESTOCK alerts.
PRODUCT_STATE_FILE = "productos_estado_english_search.json"

# Maximum time for a store search page to respond.
PAGE_TIMEOUT_MS = 15000

# Wait for JavaScript product cards to render after the search page loads.
MIN_WAIT_AFTER_PAGE_LOAD_MS = 1000
MAX_WAIT_AFTER_PAGE_LOAD_MS = 1600

# Full-store scan frequency.
MIN_WAIT_BETWEEN_CYCLES_SECONDS = 60
MAX_WAIT_BETWEEN_CYCLES_SECONDS = 75

# Maximum target-product candidates accepted from one store per cycle.
MAX_PRODUCTS_PER_STORE = 40

# Maximum product records stored in the local JSON state file.
MAX_SAVED_PRODUCT_STATES = 5000

# Important behavior:
#
# Some stores do not show a visible Add-to-Cart button or stock quantity on
# search-result cards. Versions 1 and 2 worked by treating a matching listing
# as available. Keep this True to preserve that behavior.
#
# If a product card explicitly says "Agotado", "Sin stock", "Out of stock",
# or has a disabled Add-to-Cart button, it is still treated as unavailable.
ASSUME_LISTING_PRESENT_IS_IN_STOCK = True

# Number of consecutive successful store scans where a tracked product must
# be absent before it is marked out of stock.
#
# 1 is fastest for restocks. Raise to 2 if a store often fails to render cards
# even when the HTTP response is successful.
MISSING_SCANS_TO_MARK_OUT_OF_STOCK = 1


# ============================================================
# STORE SEARCH CONFIGURATION
# ============================================================

TIENDAS = {
    "Plaza Vea": {
        "url": "https://www.plazavea.com.pe/search?q=pokemon+tcg",
        "base": "https://www.plazavea.com.pe",
    },
    "Saga Falabella": {
        "url": "https://www.falabella.com.pe/falabella-pe/search?Ntt=pokemon+30",
        "base": "https://www.falabella.com.pe",
    },
    "Ripley": {
        "url": "https://simple.ripley.com.pe/search/pokemon+tcg",
        "base": "https://simple.ripley.com.pe",
    },
    "Tai Loy": {
        "url": "https://www.tailoy.com.pe/catalogsearch/result/?q=pokemon+tcg",
        "base": "https://www.tailoy.com.pe",
    },
    "Phantom": {
        "url": "https://www.phantom.pe/catalogsearch/result/?q=pokemon+tcg",
        "base": "https://www.phantom.pe",
    },
    "LawGamers": {
        "url": "https://www.lawgamers.com/?s=pokemon+tcg&post_type=product",
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
# TARGET PRODUCT MATCHING
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


def texto_error_corto(error, limite=180):
    """Returns a compact one-line error description."""
    texto = str(error).replace("\n", " ").strip()

    if len(texto) > limite:
        return texto[:limite] + "..."

    return texto or "Unknown error"


def fecha_utc_actual():
    """Returns the current UTC time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def canonicalizar_link(link):
    """
    Removes fragments and query strings so tracking parameters do not make
    the same product look like different products.
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
    """
    Requires an explicit English-language marker.

    Examples:
    - English
    - Inglés
    - Ingles
    """
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
    Matches product naming variations including:

    - 30.º Aniversario
    - 30° Aniversario
    - 30 Aniversario
    - 30th Anniversary
    - 30th Celebration
    - 30th Celebrations
    - Celebración 30
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


def es_producto_objetivo(texto):
    """
    Requires all of the following:

    - Pokémon
    - English / Inglés
    - 30th Anniversary or 30th Celebration wording
    - Mini Tin, Booster Bundle, Binder Collection, or ETB
    """
    texto_normalizado = normalizar_texto(texto)

    if "pokemon" not in texto_normalizado:
        return False

    if not es_producto_ingles(texto_normalizado):
        return False

    if not es_producto_30_aniversario_o_celebration(texto_normalizado):
        return False

    return any(
        re.search(patron, texto_normalizado)
        for patron in TARGET_PRODUCT_PATTERNS
    )


def obtener_titulo(texto_ancla, texto_tarjeta):
    """
    Uses matching card/anchor text to choose the best human-readable title.
    """
    lineas = []

    for origen in (texto_ancla, texto_tarjeta):
        lineas.extend(
            linea.strip()
            for linea in origen.split("\n")
            if linea.strip()
        )

    for linea in lineas:
        normalizado = normalizar_texto(linea)

        if len(linea) < 10:
            continue

        if es_producto_objetivo(linea):
            return linea

        if "pokemon" in normalizado and len(linea) < 300:
            return linea

    for linea in lineas:
        if len(linea) >= 10 and len(linea) < 300:
            return linea

    return "Pokémon TCG product"


def obtener_precio(texto):
    """Finds a displayed Peruvian Sol price when present."""
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
    """
    State example:

    {
      "https://phantom.pe/product.html": {
        "tienda": "Phantom",
        "titulo": "Pokémon TCG 30th Celebration Mini Tin (Inglés)",
        "precio": "S/ 99.90",
        "link": "https://...",
        "in_stock": true,
        "missing_scans": 0,
        "last_seen_utc": "...",
        "last_stock_change_utc": "..."
      }
    }
    """
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
    """Atomically writes product state to disk."""
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


def registrar_producto_visto(estados, producto, in_stock):
    """
    Records a product observed during a successful store scrape.

    `in_stock` must be True or False when explicitly known. A product found
    on the search page may be treated as in stock when the card does not show
    an explicit availability label, depending on the configuration.
    """
    clave = producto["state_key"]
    anterior = estados.get(clave, {})
    ahora = fecha_utc_actual()

    nuevo_estado = {
        "tienda": producto["tienda"],
        "titulo": producto["titulo"],
        "precio": producto["precio"],
        "link": producto["link"],
        "in_stock": in_stock,
        "missing_scans": 0,
        "last_seen_utc": ahora,
    }

    if anterior.get("in_stock") != in_stock:
        nuevo_estado["last_stock_change_utc"] = ahora
    else:
        nuevo_estado["last_stock_change_utc"] = anterior.get(
            "last_stock_change_utc",
            ahora,
        )

    estados[clave] = nuevo_estado


def clasificar_evento_stock(estados, producto, in_stock):
    """
    Returns:
    - NEW STOCK: first confirmed/assumed available listing
    - RESTOCK: previously known unavailable item returned to listing/stock
    - None: item was already known as available
    """
    if in_stock is not True:
        return None

    anterior = estados.get(producto["state_key"])

    if anterior is None:
        return "NEW STOCK"

    if anterior.get("in_stock") is False:
        return "RESTOCK"

    return None


def marcar_productos_faltantes_como_agotados(
    estados,
    tienda,
    links_vistos,
):
    """
    Called only after a successful search-page scrape.

    If a tracked product from this store is absent from a successful result
    page for the configured number of consecutive cycles, it is marked out of
    stock. If it later reappears, it creates a RESTOCK alert.

    This is intentionally not called after timeouts, 403, 524, Cloudflare
    pages, or scraper errors.
    """
    cambios = 0
    ahora = fecha_utc_actual()

    for clave, estado in estados.items():
        if estado.get("tienda") != tienda:
            continue

        if clave in links_vistos:
            continue

        if estado.get("in_stock") is not True:
            continue

        faltantes = int(estado.get("missing_scans", 0)) + 1
        estado["missing_scans"] = faltantes

        if faltantes >= MISSING_SCANS_TO_MARK_OUT_OF_STOCK:
            estado["in_stock"] = False
            estado["last_stock_change_utc"] = ahora

            print(
                f"{tienda}: Marked OUT OF STOCK after "
                f"{faltantes} successful missing scan(s): "
                f"{estado.get('titulo', clave)}"
            )

        cambios += 1

    return cambios


# ============================================================
# DISCORD WEBHOOK
# ============================================================

async def enviar_discord(webhook_url, tienda, eventos):
    """
    Sends Discord notifications for NEW STOCK and RESTOCK events.

    Returns True only when Discord accepted every outbound message.
    """
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

        request = urllib.request.Request(
            webhook_url,
            data=datos,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "PKScrapCatalogMonitor/1.0",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=20) as respuesta:
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
# SEARCH-PAGE SCRAPING
# ============================================================

def es_pagina_error_o_bloqueo(titulo, texto_pagina):
    """
    Detects common Cloudflare/origin errors. When detected, the store scan is
    treated as failed, so existing product states are never changed to
    out-of-stock because of an error page.
    """
    texto = normalizar_texto(
        f"{titulo}\n{texto_pagina[:3000]}"
    )

    indicadores_error = (
        "error code 524",
        "a timeout occurred",
        "error code 403",
        "access denied",
        "just a moment",
        "checking your browser",
        "cloudflare",
        "temporarily unavailable",
        "service unavailable",
    )

    return any(
        indicador in texto
        for indicador in indicadores_error
    )


async def scrapear_resultados_tienda(page, tienda, datos):
    """
    Scrapes matching product cards from the store's live search/catalog page.

    This does not use hardcoded individual product URLs. Each product link is
    discovered from the store page during every scan.
    """
    respuesta = await page.goto(
        datos["url"],
        timeout=PAGE_TIMEOUT_MS,
        wait_until="domcontentloaded",
    )

    if respuesta is not None and respuesta.status >= 400:
        raise RuntimeError(
            f"HTTP {respuesta.status} loading store search page"
        )

    await page.wait_for_timeout(
        random.randint(
            MIN_WAIT_AFTER_PAGE_LOAD_MS,
            MAX_WAIT_AFTER_PAGE_LOAD_MS,
        )
    )

    snapshot = await page.evaluate(
        """
        () => {
            const visible = (element) => Boolean(
                element.offsetWidth ||
                element.offsetHeight ||
                element.getClientRects().length
            );

            const cleanText = (text) =>
                (text || "")
                    .replace(/\\s+/g, " ")
                    .trim();

            const getCard = (anchor) => {
                let node = anchor;
                let best = null;

                for (let level = 0; level < 7; level += 1) {
                    node = node.parentElement;

                    if (!node) {
                        break;
                    }

                    const text = cleanText(node.innerText || "");
                    const className = String(node.className || "");

                    if (
                        text.length < 8 ||
                        text.length > 2500
                    ) {
                        continue;
                    }

                    let score = 0;
                    const classLower = className.toLowerCase();

                    if (
                        node.tagName === "ARTICLE" ||
                        node.tagName === "LI"
                    ) {
                        score += 20;
                    }

                    if (
                        classLower.includes("product") ||
                        classLower.includes("card") ||
                        classLower.includes("item")
                    ) {
                        score += 15;
                    }

                    if (
                        /s\\/\\s*[\\d,.]+/i.test(text)
                    ) {
                        score += 8;
                    }

                    if (
                        /agregar al carrito|add to cart|agotado|sin stock|disponible/i.test(text)
                    ) {
                        score += 8;
                    }

                    score -= level;

                    if (
                        !best ||
                        score > best.score
                    ) {
                        best = {
                            text,
                            node,
                            score
                        };
                    }
                }

                return best || {
                    text: cleanText(anchor.innerText || ""),
                    node: anchor,
                    score: 0
                };
            };

            const anchors = Array.from(
                document.querySelectorAll("a[href]")
            );

            const products = [];

            for (const anchor of anchors) {
                const anchorText = cleanText(anchor.innerText || "");
                const href = anchor.getAttribute("href") || "";

                if (
                    !href ||
                    anchorText.length < 5 ||
                    anchorText.length > 500
                ) {
                    continue;
                }

                const card = getCard(anchor);

                const actionSelector = [
                    "button",
                    "input[type='button']",
                    "input[type='submit']",
                    "[role='button']"
                ].join(",");

                const buttons = Array.from(
                    card.node.querySelectorAll(actionSelector)
                ).map((element) => ({
                    text: cleanText(
                        element.innerText ||
                        element.value ||
                        element.getAttribute("aria-label") ||
                        element.textContent ||
                        ""
                    ),
                    disabled: Boolean(
                        element.disabled ||
                        element.getAttribute("aria-disabled") === "true" ||
                        element.classList.contains("disabled")
                    ),
                    visible: visible(element)
                })).filter((button) => button.text);

                products.push({
                    href,
                    anchorText,
                    cardText: card.text,
                    buttons
                });
            }

            return {
                title: document.title || "",
                bodyText: document.body && document.body.innerText
                    ? document.body.innerText
                    : "",
                products
            };
        }
        """
    )

    if es_pagina_error_o_bloqueo(
        snapshot.get("title", ""),
        snapshot.get("bodyText", ""),
    ):
        raise RuntimeError(
            "Store returned a Cloudflare, timeout, access-denied, "
            "or service-error page"
        )

    productos = []
    links_vistos = set()

    for item in snapshot.get("products", []):
        href = item.get("href", "")
        texto_ancla = item.get("anchorText", "")
        texto_tarjeta = item.get("cardText", "")

        if href.startswith((
            "#",
            "javascript:",
            "mailto:",
            "tel:",
        )):
            continue

        texto_completo = f"{texto_ancla}\n{texto_tarjeta}"

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

        in_stock, fuente_stock = detectar_stock_tarjeta(
            texto_tarjeta,
            item.get("buttons", []),
        )

        productos.append({
            "tienda": tienda,
            "titulo": titulo,
            "precio": precio,
            "link": link,
            "state_key": state_key,
            "in_stock": in_stock,
            "stock_source": fuente_stock,
        })

        if len(productos) >= MAX_PRODUCTS_PER_STORE:
            break

    return productos


def detectar_stock_tarjeta(texto_tarjeta, botones):
    """
    Determines availability strictly from the scraped catalog card.

    Return values:
    - (True,  "...")  confirmed/assumed available
    - (False, "...")  explicitly unavailable
    - (None,  "...")  unknown, only possible if listing fallback is disabled
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
        return True, "enabled add-to-cart button in search card"

    if botones_carrito and all(
        boton.get("disabled")
        for boton in botones_carrito
    ):
        return False, "disabled add-to-cart button in search card"

    if any(
        re.search(patron, texto)
        for patron in patrones_no_stock
    ):
        return False, "explicit out-of-stock text in search card"

    if any(
        re.search(patron, texto)
        for patron in patrones_stock
    ):
        return True, "explicit stock text in search card"

    if ASSUME_LISTING_PRESENT_IS_IN_STOCK:
        return True, "matching live search listing"

    return None, "stock status unknown"


# ============================================================
# SCAN CYCLE
# ============================================================

async def ejecutar_ciclo(browser, estados, webhook_url):
    """
    Scrapes each store catalog/search page, updates availability state, and
    sends Discord alerts for new listings and listing reappearances.
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
                productos = await scrapear_resultados_tienda(
                    page,
                    tienda,
                    datos,
                )

            except Exception as error:
                print(
                    f"{tienda}: Search scrape failed; state not changed: "
                    f"{texto_error_corto(error)}"
                )
                continue

            links_vistos = {
                producto["state_key"]
                for producto in productos
            }

            print(
                f"{tienda}: Scraped {len(productos)} matching "
                "target product listing(s)."
            )

            eventos_pendientes = []
            estado_cambio = False

            for producto in productos:
                in_stock = producto["in_stock"]

                if in_stock is False:
                    registrar_producto_visto(
                        estados,
                        producto,
                        False,
                    )

                    estado_cambio = True

                    print(
                        f"{tienda}: OUT OF STOCK: "
                        f"{producto['titulo']}"
                    )

                    continue

                if in_stock is None:
                    print(
                        f"{tienda}: Stock unknown, ignored: "
                        f"{producto['titulo']}"
                    )
                    continue

                tipo_evento = clasificar_evento_stock(
                    estados,
                    producto,
                    True,
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
                    registrar_producto_visto(
                        estados,
                        producto,
                        True,
                    )

                    estado_cambio = True

                    print(
                        f"{tienda}: Still available; no repeat alert: "
                        f"{producto['titulo']}"
                    )

            cambios_faltantes = marcar_productos_faltantes_como_agotados(
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

            discord_enviado = await enviar_discord(
                webhook_url,
                tienda,
                eventos_pendientes,
            )

            if discord_enviado:
                for evento in eventos_pendientes:
                    registrar_producto_visto(
                        estados,
                        evento["producto"],
                        True,
                    )

                guardar_estados_productos(estados)

                print(
                    f"{tienda}: Discord sent "
                    f"{len(eventos_pendientes)} alert(s)."
                )
            else:
                print(
                    f"{tienda}: Discord failed. Products remain "
                    "eligible for retry during the next cycle."
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

    print("=" * 72)
    print("PKSCRAP SEARCH SCRAPER — ENGLISH POKEMON 30TH RESTOCKS")
    print(f"Configured stores: {len(TIENDAS)}")
    print(f"Saved product states: {len(estados)}")
    print("Target products:")
    print("  - Mini Tin")
    print("  - Booster Bundle")
    print("  - Binder Collection")
    print("  - Elite Trainer Box / ETB")
    print("Detection: live store search/catalog page scraping.")
    print("Alerts: NEW STOCK and RESTOCK only.")
    print("=" * 72)

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
