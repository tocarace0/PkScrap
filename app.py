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

# This file saves each product's last confirmed stock state.
# It is used to detect explicit out-of-stock -> in-stock restocks.
PRODUCT_STATE_FILE = "productos_estado_english_targeted.json"

# Page timeout.
PAGE_TIMEOUT_MS = 20000

# Delay after loading store search pages.
MIN_WAIT_BETWEEN_STORES_MS = 1200
MAX_WAIT_BETWEEN_STORES_MS = 2200

# Delay after loading individual product pages for stock verification.
MIN_WAIT_PRODUCT_PAGE_MS = 900
MAX_WAIT_PRODUCT_PAGE_MS = 1600

# Time between complete scans.
MIN_WAIT_BETWEEN_CYCLES_SECONDS = 90
MAX_WAIT_BETWEEN_CYCLES_SECONDS = 150

# Safety limit: do not open too many candidate product pages per store.
MAX_PRODUCTS_TO_VERIFY_PER_STORE = 30

# Maximum product states saved in the JSON file.
MAX_SAVED_PRODUCT_STATES = 5000


# ============================================================
# STORE CONFIGURATION
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
# TARGET PRODUCTS
# ============================================================

# The product must match one of these product families.
TARGET_PRODUCT_PATTERNS = (
    r"\bmini\s*tin\b",
    r"\bbooster\s*bundle\b",
    r"\bbinder\s*collection\b",
    r"\belite\s*trainer\s*box\b",
    r"\betb\b",
)


# ============================================================
# TEXT HELPERS
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
    """Returns a compact, one-line error message."""
    texto = str(error).replace("\n", " ").strip()

    if len(texto) > limite:
        return texto[:limite] + "..."

    return texto or "Unknown error"


def fecha_utc_actual():
    """Returns an ISO UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def canonicalizar_link(link):
    """
    Removes fragments and query-string tracking parameters so one product URL
    has one persistent state record.
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

    Examples accepted:
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
    Recognizes Pokémon's 30th Anniversary / 30th Celebration variants.

    Examples accepted:
    - 30 Aniversario
    - 30.º Aniversario
    - 30° Aniversario
    - 30th Anniversary
    - 30th Celebration
    - 30th Celebrations
    - Aniversario 30
    - Celebration 30
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
        r"\b20\s*(?:th|[.\u00ba\u00b0o]+)?\s*aniversario\b",
        r"\b20\s*(?:th|[.\u00ba\u00b0o]+)?\s*anniversary\b",
        r"\b25\s*(?:th|[.\u00ba\u00b0o]+)?\s*aniversario\b",
        r"\b25\s*(?:th|[.\u00ba\u00b0o]+)?\s*anniversary\b",
        r"\b20th\b",
        r"\b25th\b",
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
    Returns True only for the requested English 30th products:

    - Mini Tin
    - Booster Bundle
    - Binder Collection
    - Elite Trainer Box / ETB
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


def obtener_titulo(lineas):
    """Attempts to select the actual product title from a product card."""
    for linea in lineas:
        texto = linea.strip()
        texto_normalizado = normalizar_texto(texto)

        if len(texto) < 8:
            continue

        if "s/" in texto.lower():
            continue

        if "precio" in texto_normalizado:
            continue

        if "pokemon" in texto_normalizado:
            return texto

    for linea in lineas:
        texto = linea.strip()
        texto_normalizado = normalizar_texto(texto)

        if len(texto) < 8:
            continue

        if "s/" in texto.lower():
            continue

        if "precio" in texto_normalizado:
            continue

        return texto

    return lineas[0].strip()


def obtener_precio(lineas):
    """Finds a Peruvian Sol price in card text if available."""
    for linea in lineas:
        if "s/" in linea.lower():
            return linea.strip()

    for linea in lineas:
        if "precio" in normalizar_texto(linea):
            return linea.strip()

    return "Precio no disponible"


# ============================================================
# PRODUCT STATE STORAGE
# ============================================================

def cargar_estados_productos():
    """
    Loads previously observed product stock states.

    Format:
    {
      "https://store/product": {
        "tienda": "Phantom",
        "titulo": "Pokémon TCG 30th Celebration Mini Tin (Inglés)",
        "precio": "S/ 99.90",
        "link": "https://...",
        "in_stock": true,
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
    """Atomically saves product state data."""
    try:
        if len(estados) > MAX_SAVED_PRODUCT_STATES:
            claves_ordenadas = sorted(
                estados,
                key=lambda clave: estados[clave].get(
                    "last_seen_utc",
                    "",
                ),
            )

            exceso = len(estados) - MAX_SAVED_PRODUCT_STATES

            for clave in claves_ordenadas[:exceso]:
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


def clasificar_evento_stock(estados, producto, in_stock):
    """
    Returns:
    - NEW STOCK: first confirmed in-stock observation
    - RESTOCK: last confirmed state was out of stock and it is now in stock
    - None: unchanged stock or unknown/unconfirmed stock
    """
    if in_stock is not True:
        return None

    estado_anterior = estados.get(producto["state_key"])

    if estado_anterior is None:
        return "NEW STOCK"

    stock_anterior = estado_anterior.get("in_stock")

    if stock_anterior is False:
        return "RESTOCK"

    if stock_anterior is None:
        return "NEW STOCK"

    return None


def actualizar_estado_producto(estados, producto, in_stock):
    """
    Saves confirmed state.

    If stock is unknown, retain the prior confirmed availability state rather
    than falsely treating a page-load problem as an out-of-stock transition.
    """
    clave = producto["state_key"]
    anterior = estados.get(clave, {})
    ahora = fecha_utc_actual()

    estado_nuevo = {
        "tienda": producto["tienda"],
        "titulo": producto["titulo"],
        "precio": producto["precio"],
        "link": producto["link"],
        "last_seen_utc": ahora,
    }

    if in_stock is None:
        estado_nuevo["in_stock"] = anterior.get("in_stock")
        estado_nuevo["last_stock_change_utc"] = anterior.get(
            "last_stock_change_utc"
        )
    else:
        estado_previo_stock = anterior.get("in_stock")

        estado_nuevo["in_stock"] = in_stock

        if estado_previo_stock != in_stock:
            estado_nuevo["last_stock_change_utc"] = ahora
        else:
            estado_nuevo["last_stock_change_utc"] = anterior.get(
                "last_stock_change_utc",
                ahora,
            )

    estados[clave] = estado_nuevo


# ============================================================
# DISCORD NOTIFICATIONS
# ============================================================

async def enviar_discord(webhook_url, tienda, eventos):
    """
    Sends Discord alerts for confirmed NEW STOCK and RESTOCK events.

    Returns True only if every Discord message was accepted.
    """
    bloques = []

    for evento in eventos:
        producto = evento["producto"]
        tipo_evento = evento["tipo_evento"]

        bloques.append(
            f"**{tipo_evento} — {producto['titulo']}**\n"
            f"Price: {producto['precio']}\n"
            f"Store: {tienda}\n"
            f"Language: English\n"
            f"Link: {producto['link']}"
        )

    encabezado = (
        f"**POKÉMON TCG 30TH ENGLISH STOCK ALERT — {tienda}**\n"
        f"Confirmed events: {len(eventos)}\n\n"
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
                "User-Agent": "PKScrapTargetedRestockMonitor/1.0",
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
            estado_http = await asyncio.to_thread(publicar, payload)

            if estado_http not in (200, 204):
                print(
                    f"[ERROR] Discord returned unexpected HTTP status "
                    f"{estado_http}"
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
# PRODUCT SEARCH
# ============================================================

async def buscar_productos_candidatos(page, tienda, datos):
    """
    Searches store result pages for the requested English product families.

    Availability is verified separately on the actual product page.
    """
    await page.goto(
        datos["url"],
        timeout=PAGE_TIMEOUT_MS,
        wait_until="domcontentloaded",
    )

    await page.wait_for_timeout(
        random.randint(
            MIN_WAIT_BETWEEN_STORES_MS,
            MAX_WAIT_BETWEEN_STORES_MS,
        )
    )

    enlaces = await page.evaluate(
        """
        () => {
            const items = [];
            const links = Array.from(document.querySelectorAll("a"));

            for (const link of links) {
                const text = link.innerText
                    ? link.innerText.trim()
                    : "";

                const href = link.getAttribute("href") || "";

                if (
                    text.length > 5 &&
                    text.length < 500 &&
                    href
                ) {
                    items.push({
                        text: text,
                        href: href
                    });
                }
            }

            return items;
        }
        """
    )

    productos = []
    enlaces_vistos = set()
    titulos_vistos = set()

    for item in enlaces:
        texto = item.get("text", "")
        href = item.get("href", "")

        if not href:
            continue

        if href.startswith((
            "#",
            "javascript:",
            "mailto:",
            "tel:",
        )):
            continue

        link = urljoin(datos["base"], href)
        state_key = canonicalizar_link(link)

        if not state_key:
            continue

        if state_key in enlaces_vistos:
            continue

        lineas = [
            linea.strip()
            for linea in texto.split("\n")
            if linea.strip()
        ]

        if not lineas:
            continue

        texto_completo = " ".join(lineas)

        if not es_producto_objetivo(texto_completo):
            continue

        titulo = obtener_titulo(lineas)
        precio = obtener_precio(lineas)

        clave_titulo = normalizar_texto(titulo)

        if clave_titulo in titulos_vistos:
            continue

        enlaces_vistos.add(state_key)
        titulos_vistos.add(clave_titulo)

        productos.append({
            "tienda": tienda,
            "titulo": titulo,
            "precio": precio,
            "link": link,
            "state_key": state_key,
        })

        if len(productos) >= MAX_PRODUCTS_TO_VERIFY_PER_STORE:
            break

    return productos


# ============================================================
# PRODUCT-PAGE STOCK VERIFICATION
# ============================================================

async def verificar_stock_producto(page, producto):
    """
    Opens the actual product page and checks explicit stock signals.

    Returns:
    - True: confirmed in stock
    - False: confirmed out of stock
    - None: could not safely determine stock status

    Strongest signals:
    1. Visible enabled "Add to Cart" button -> True
    2. Visible disabled "Add to Cart" button -> False
    3. Product-page stock status element -> True / False
    4. Explicit stock text / out-of-stock text -> True / False
    """
    await page.goto(
        producto["link"],
        timeout=PAGE_TIMEOUT_MS,
        wait_until="domcontentloaded",
    )

    await page.wait_for_timeout(
        random.randint(
            MIN_WAIT_PRODUCT_PAGE_MS,
            MAX_WAIT_PRODUCT_PAGE_MS,
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

            const actionSelector = [
                "#product-addtocart-button",
                "[id*='addtocart']",
                "[class*='addtocart']",
                "button",
                "input[type='button']",
                "input[type='submit']",
                "[role='button']",
                "a[role='button']"
            ].join(",");

            const stockSelector = [
                ".stock",
                ".stock.available",
                ".stock.unavailable",
                "[class*='stock']",
                "[class*='availability']",
                "[data-testid*='stock']",
                "[data-testid*='availability']"
            ].join(",");

            const actions = Array.from(
                document.querySelectorAll(actionSelector)
            ).map((element) => ({
                text: (
                    element.innerText ||
                    element.value ||
                    element.getAttribute("aria-label") ||
                    element.textContent ||
                    ""
                ).trim(),
                disabled: Boolean(
                    element.disabled ||
                    element.getAttribute("aria-disabled") === "true" ||
                    element.classList.contains("disabled")
                ),
                visible: visible(element),
                className: String(element.className || "")
            })).filter((item) => item.text);

            const stockElements = Array.from(
                document.querySelectorAll(stockSelector)
            ).map((element) => ({
                text: (
                    element.innerText ||
                    element.getAttribute("aria-label") ||
                    element.textContent ||
                    ""
                ).trim(),
                className: String(element.className || ""),
                visible: visible(element)
            })).filter((item) => item.visible && item.text);

            return {
                title: document.title || "",
                bodyText: document.body && document.body.innerText
                    ? document.body.innerText
                    : "",
                actions,
                stockElements
            };
        }
        """
    )

    texto_total = normalizar_texto(
        f"{snapshot.get('title', '')}\n{snapshot.get('bodyText', '')}"
    )

    acciones = snapshot.get("actions", [])
    stock_elements = snapshot.get("stockElements", [])

    patrones_agregar_carrito = (
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

    for accion in acciones:
        if not accion.get("visible"):
            continue

        texto_accion = normalizar_texto(accion.get("text", ""))

        if any(
            re.search(patron, texto_accion)
            for patron in patrones_agregar_carrito
        ):
            botones_carrito.append(accion)

    # A visible enabled cart button is the strongest positive signal.
    if any(
        not boton.get("disabled")
        for boton in botones_carrito
    ):
        return True

    # A visible disabled cart button is an explicit unavailable signal.
    if botones_carrito and all(
        boton.get("disabled")
        for boton in botones_carrito
    ):
        return False

    # Check dedicated product stock/availability elements before generic body text.
    for elemento in stock_elements:
        texto_elemento = normalizar_texto(elemento.get("text", ""))
        clase_elemento = normalizar_texto(elemento.get("className", ""))

        if any(
            re.search(patron, texto_elemento)
            for patron in patrones_no_stock
        ):
            return False

        if "unavailable" in clase_elemento:
            return False

        if any(
            re.search(patron, texto_elemento)
            for patron in patrones_stock
        ):
            return True

        if "available" in clase_elemento:
            return True

    # Generic product-page body fallback.
    if any(
        re.search(patron, texto_total)
        for patron in patrones_no_stock
    ):
        return False

    if any(
        re.search(patron, texto_total)
        for patron in patrones_stock
    ):
        return True

    return None


# ============================================================
# SCAN CYCLE
# ============================================================

async def ejecutar_ciclo(browser, estados, webhook_url):
    """
    Searches every store, verifies actual product-page stock, stores stock
    transitions, and alerts Discord only for new stock and restocks.
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

    search_page = await context.new_page()
    product_page = await context.new_page()

    try:
        for tienda, datos in TIENDAS.items():
            try:
                productos = await buscar_productos_candidatos(
                    search_page,
                    tienda,
                    datos,
                )

                if not productos:
                    print(
                        f"{tienda}: No matching English Mini Tin, "
                        "Booster Bundle, Binder Collection, or ETB found."
                    )
                    continue

                print(
                    f"{tienda}: Found {len(productos)} target product "
                    "candidate(s). Verifying stock..."
                )

                eventos_pendientes = []
                estado_modificado = False

                for producto in productos:
                    try:
                        in_stock = await verificar_stock_producto(
                            product_page,
                            producto,
                        )

                    except Exception as error:
                        print(
                            f"{tienda}: Product stock verification failed "
                            f"for '{producto['titulo']}': "
                            f"{texto_error_corto(error)}"
                        )
                        continue

                    if in_stock is None:
                        print(
                            f"{tienda}: Stock status UNKNOWN: "
                            f"{producto['titulo']}"
                        )

                        actualizar_estado_producto(
                            estados,
                            producto,
                            None,
                        )

                        estado_modificado = True
                        continue

                    if in_stock is False:
                        print(
                            f"{tienda}: OUT OF STOCK: "
                            f"{producto['titulo']}"
                        )

                        actualizar_estado_producto(
                            estados,
                            producto,
                            False,
                        )

                        estado_modificado = True
                        continue

                    tipo_evento = clasificar_evento_stock(
                        estados,
                        producto,
                        True,
                    )

                    if tipo_evento:
                        print(
                            f"{tienda}: {tipo_evento}: "
                            f"{producto['titulo']}"
                        )

                        eventos_pendientes.append({
                            "tipo_evento": tipo_evento,
                            "producto": producto,
                        })
                    else:
                        print(
                            f"{tienda}: STILL IN STOCK, no repeated alert: "
                            f"{producto['titulo']}"
                        )

                        actualizar_estado_producto(
                            estados,
                            producto,
                            True,
                        )

                        estado_modificado = True

                # Save out-of-stock and unchanged in-stock states immediately.
                if estado_modificado:
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
                        actualizar_estado_producto(
                            estados,
                            evento["producto"],
                            True,
                        )

                    guardar_estados_productos(estados)

                    print(
                        f"{tienda}: Discord sent "
                        f"{len(eventos_pendientes)} notification(s)."
                    )
                else:
                    print(
                        f"{tienda}: Discord failed. Matching in-stock "
                        "products will be retried next cycle."
                    )

            except Exception as error:
                print(
                    f"{tienda}: Store scan error: "
                    f"{texto_error_corto(error)}"
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

    if not webhook_url.startswith(
        "https://discord.com/api/webhooks/"
    ):
        print(
            "[WARNING] DISCORD_WEBHOOK_URL does not look like "
            "a standard Discord webhook URL."
        )

    estados = cargar_estados_productos()

    print("=" * 72)
    print("PKSCRAP TARGETED ENGLISH POKEMON 30TH RESTOCK MONITOR")
    print(f"Configured stores: {len(TIENDAS)}")
    print(f"Saved product states: {len(estados)}")
    print("Target products:")
    print("  - Mini Tin")
    print("  - Booster Bundle")
    print("  - Binder Collection")
    print("  - Elite Trainer Box / ETB")
    print("Alerts: NEW STOCK and explicit OUT OF STOCK -> RESTOCK only.")
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
