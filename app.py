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

# This file records whether each matching product was last explicitly
# confirmed in stock, out of stock, or unknown.
PRODUCT_STATE_FILE = "productos_estado_english.json"

# Only English Pokémon 30th Anniversary products are monitored.
REQUIRE_30TH_ANNIVERSARY = True

# Maximum time for a store or product page to load.
PAGE_TIMEOUT_MS = 20000

# Delay after loading a search-result page.
MIN_WAIT_BETWEEN_STORES_MS = 1200
MAX_WAIT_BETWEEN_STORES_MS = 2200

# Delay after loading an individual product page to check stock.
MIN_WAIT_PRODUCT_PAGE_MS = 900
MAX_WAIT_PRODUCT_PAGE_MS = 1600

# Time between full scans.
MIN_WAIT_BETWEEN_CYCLES_SECONDS = 90
MAX_WAIT_BETWEEN_CYCLES_SECONDS = 150

# Avoid opening an excessive number of product pages if a store search
# page returns duplicate or malformed matches.
MAX_PRODUCTS_TO_VERIFY_PER_STORE = 25

# Maximum saved product states.
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

    return texto or "Error desconocido"


def fecha_utc_actual():
    """Returns a readable UTC timestamp for the state file."""
    return datetime.now(timezone.utc).isoformat()


def canonicalizar_link(link):
    """
    Removes URL fragments and query strings used for tracking so the same
    product does not create multiple state entries because of URL parameters.
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
    Returns True only if the listing explicitly identifies the product as
    English or Inglés.
    """
    texto_normalizado = normalizar_texto(texto)

    indicadores_ingles = (
        "english",
        "ingles",
    )

    return any(
        indicador in texto_normalizado
        for indicador in indicadores_ingles
    )


def es_producto_30_aniversario(texto):
    """
    Matches common Pokémon 30th Anniversary formats, including:

    - 30 Aniversario
    - 30.º Aniversario
    - 30° Aniversario
    - 30o Aniversario
    - 30th Anniversary
    - Aniversario 30
    - Colección 30
    - Collection 30
    """
    texto_normalizado = normalizar_texto(texto)

    patrones_validos = (
        r"\b30\s*(?:th|[.\u00ba\u00b0o]+)?\s*aniversario\b",
        r"\b30th\s+anniversary\b",
        r"\baniversario\s*(?:[.\u00ba\u00b0o]+)?\s*30\b",
        r"\bcoleccion\s*(?:[.\u00ba\u00b0o]+)?\s*30\b",
        r"\bcollection\s*(?:[.\u00ba\u00b0o]+)?\s*30\b",
    )

    patrones_falsos = (
        r"\b20\s*(?:th|[.\u00ba\u00b0o]+)?\s*aniversario\b",
        r"\b25\s*(?:th|[.\u00ba\u00b0o]+)?\s*aniversario\b",
        r"\b20th\b",
        r"\b25th\b",
        r"\b20\s*aniversario\b",
        r"\b25\s*aniversario\b",
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


def obtener_titulo(lineas):
    """
    Attempts to choose the actual product title rather than a price,
    stock label, or generic store text.
    """
    for linea in lineas:
        linea_normalizada = normalizar_texto(linea)

        if len(linea.strip()) < 8:
            continue

        if "s/" in linea.lower():
            continue

        if "precio" in linea_normalizada:
            continue

        if "pokemon" in linea_normalizada:
            return linea.strip()

    for linea in lineas:
        linea_normalizada = normalizar_texto(linea)

        if len(linea.strip()) < 8:
            continue

        if "s/" in linea.lower():
            continue

        if "precio" in linea_normalizada:
            continue

        return linea.strip()

    return lineas[0].strip()


def obtener_precio(lineas):
    """Attempts to find the price displayed in a product card."""
    for linea in lineas:
        if "s/" in linea.lower():
            return linea.strip()

    for linea in lineas:
        if "precio" in normalizar_texto(linea):
            return linea.strip()

    return "Precio no disponible"


# ============================================================
# PRODUCT-STATE FILE
# ============================================================

def cargar_estados_productos():
    """
    Loads product states.

    State format:
    {
      "canonical-product-url": {
        "tienda": "Plaza Vea",
        "titulo": "...",
        "precio": "S/ 99.90",
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
    """Atomically saves product stock states."""
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


def actualizar_estado_producto(estados, producto, in_stock):
    """
    Updates stored state after a confirmed scan result.

    Unknown stock status is stored as metadata, but it does not overwrite a
    known in-stock/out-of-stock state. That avoids a page-load issue causing
    a false restock event on the next cycle.
    """
    clave = producto["state_key"]
    estado_anterior = estados.get(clave, {})
    ahora = fecha_utc_actual()

    nuevo_estado = {
        "tienda": producto["tienda"],
        "titulo": producto["titulo"],
        "precio": producto["precio"],
        "link": producto["link"],
        "last_seen_utc": ahora,
    }

    if in_stock is None:
        nuevo_estado["in_stock"] = estado_anterior.get("in_stock")
        nuevo_estado["last_stock_change_utc"] = estado_anterior.get(
            "last_stock_change_utc"
        )
    else:
        estado_previo_stock = estado_anterior.get("in_stock")

        nuevo_estado["in_stock"] = in_stock

        if estado_previo_stock != in_stock:
            nuevo_estado["last_stock_change_utc"] = ahora
        else:
            nuevo_estado["last_stock_change_utc"] = estado_anterior.get(
                "last_stock_change_utc",
                ahora,
            )

    estados[clave] = nuevo_estado


def clasificar_evento_stock(estados, producto, in_stock):
    """
    Decides whether an explicit stock transition should create a Discord alert.

    Returns:
    - "NEW STOCK" if this is the first confirmed in-stock observation.
    - "RESTOCK" if last known state was explicitly out of stock.
    - None for unchanged stock, unknown stock, or first observation out of stock.
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


# ============================================================
# DISCORD
# ============================================================

async def enviar_discord(webhook_url, tienda, eventos):
    """
    Sends all new-stock/restock alerts for one store.

    Returns True only when Discord accepted every message.
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
        f"**POKEMON TCG 30TH ANNIVERSARY ENGLISH ALERT — {tienda}**\n"
        f"Confirmed stock events: {len(eventos)}\n\n"
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
                "User-Agent": "PKScrapRestockMonitor/1.0",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=20) as respuesta:
            return respuesta.status

    for contenido in mensajes:
        payload = {
            "username": "Pokemon English Restock Alert",
            "content": contenido[:1900],
            "allowed_mentions": {
                "parse": [],
            },
        }

        try:
            estado_http = await asyncio.to_thread(publicar, payload)

            if estado_http not in (200, 204):
                print(
                    f"[ERROR] Discord returned unexpected status: "
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
# STORE SEARCH AND STOCK VERIFICATION
# ============================================================

async def buscar_productos_candidatos(page, tienda, datos):
    """
    Searches a store page for possible English Pokémon 30th Anniversary
    product links. It does not decide availability yet.
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

        if not state_key or state_key in enlaces_vistos:
            continue

        lineas = [
            linea.strip()
            for linea in texto.split("\n")
            if linea.strip()
        ]

        if not lineas:
            continue

        texto_completo = " ".join(lineas)
        texto_normalizado = normalizar_texto(texto_completo)

        if "pokemon" not in texto_normalizado:
            continue

        if not es_producto_ingles(texto_completo):
            continue

        if (
            REQUIRE_30TH_ANNIVERSARY
            and not es_producto_30_aniversario(texto_completo)
        ):
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


async def verificar_stock_producto(page, producto):
    """
    Opens the actual product page and determines stock status.

    Returns:
    - True: explicit available/add-to-cart state found;
    - False: explicit unavailable/sold-out state found;
    - None: stock status could not be determined safely.

    Unknown does not produce a Discord alert and does not overwrite a prior
    confirmed stock state.
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
            const bodyText = document.body && document.body.innerText
                ? document.body.innerText
                : "";

            const selector = [
                "button",
                "input[type='button']",
                "input[type='submit']",
                "[role='button']",
                "a[role='button']"
            ].join(",");

            const actions = Array.from(
                document.querySelectorAll(selector)
            ).map((element) => {
                const text = (
                    element.innerText ||
                    element.value ||
                    element.getAttribute("aria-label") ||
                    element.textContent ||
                    ""
                ).trim();

                const disabled = Boolean(
                    element.disabled ||
                    element.getAttribute("aria-disabled") === "true" ||
                    element.classList.contains("disabled")
                );

                const visible = Boolean(
                    element.offsetWidth ||
                    element.offsetHeight ||
                    element.getClientRects().length
                );

                return {
                    text,
                    disabled,
                    visible
                };
            }).filter((action) => action.text);

            return {
                title: document.title || "",
                bodyText,
                actions
            };
        }
        """
    )

    texto_total = normalizar_texto(
        f"{snapshot.get('title', '')}\n{snapshot.get('bodyText', '')}"
    )

    acciones = snapshot.get("actions", [])

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

    patrones_stock_texto = (
        r"\ben stock\b",
        r"\bdisponible\b",
        r"\bultimas unidades\b",
        r"\bultima unidad\b",
    )

    acciones_carrito = []

    for accion in acciones:
        if not accion.get("visible"):
            continue

        texto_accion = normalizar_texto(accion.get("text", ""))

        if any(
            re.search(patron, texto_accion)
            for patron in patrones_agregar_carrito
        ):
            acciones_carrito.append(accion)

    # A visible and enabled cart button is the strongest availability signal.
    if any(not accion.get("disabled") for accion in acciones_carrito):
        return True

    # A visible but disabled cart button is an explicit unavailable state.
    if acciones_carrito and all(
        accion.get("disabled")
        for accion in acciones_carrito
    ):
        return False

    # Explicit unavailable page text is stronger than generic availability text.
    if any(
        re.search(patron, texto_total)
        for patron in patrones_no_stock
    ):
        return False

    # Some stores do not expose a button text but do show explicit stock text.
    if any(
        re.search(patron, texto_total)
        for patron in patrones_stock_texto
    ):
        return True

    return None


# ============================================================
# ONE COMPLETE SCAN CYCLE
# ============================================================

async def ejecutar_ciclo(browser, estados, webhook_url):
    """
    Scans every configured store, verifies product-page stock, records explicit
    state changes, and sends Discord alerts for new stock/restocks only.
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
                        f"{tienda}: No English 30th Anniversary "
                        "product candidates found."
                    )
                    continue

                print(
                    f"{tienda}: Found {len(productos)} English 30th "
                    "Anniversary candidate(s). Verifying stock..."
                )

                eventos_pendientes = []
                hubo_cambios_estado = False

                for producto in productos:
                    try:
                        in_stock = await verificar_stock_producto(
                            product_page,
                            producto,
                        )

                    except Exception as error:
                        print(
                            f"{tienda}: Could not verify stock for "
                            f"'{producto['titulo']}': "
                            f"{texto_error_corto(error)}"
                        )
                        continue

                    if in_stock is None:
                        print(
                            f"{tienda}: Stock unknown for "
                            f"'{producto['titulo']}'. No alert sent."
                        )

                        actualizar_estado_producto(
                            estados,
                            producto,
                            None,
                        )

                        hubo_cambios_estado = True
                        continue

                    tipo_evento = clasificar_evento_stock(
                        estados,
                        producto,
                        in_stock,
                    )

                    if in_stock is False:
                        print(
                            f"{tienda}: Explicitly out of stock: "
                            f"{producto['titulo']}"
                        )

                        actualizar_estado_producto(
                            estados,
                            producto,
                            False,
                        )

                        hubo_cambios_estado = True
                        continue

                    # Product is explicitly in stock.
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
                            f"{tienda}: Still in stock; no repeat alert: "
                            f"{producto['titulo']}"
                        )

                        actualizar_estado_producto(
                            estados,
                            producto,
                            True,
                        )

                        hubo_cambios_estado = True

                if hubo_cambios_estado:
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
                        f"{len(eventos_pendientes)} stock alert(s)."
                    )
                else:
                    print(
                        f"{tienda}: Discord failed. In-stock state was "
                        "not saved, so these alerts will retry next cycle."
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
            f"[ERROR] Missing {DISCORD_WEBHOOK_ENV_VAR}. "
            "The monitor cannot send Discord alerts."
        )
        return

    if not webhook_url.startswith(
        "https://discord.com/api/webhooks/"
    ):
        print(
            "[WARNING] DISCORD_WEBHOOK_URL does not appear to be "
            "a normal Discord webhook URL."
        )

    estados = cargar_estados_productos()

    print("=" * 68)
    print("PKSCRAP ENGLISH POKEMON 30TH ANNIVERSARY RESTOCK MONITOR")
    print(f"Configured stores: {len(TIENDAS)}")
    print(f"Saved product states: {len(estados)}")
    print("Alerts: first confirmed stock and explicit restocks only.")
    print("Press Ctrl+C to stop when running manually.")
    print("=" * 68)

    ciclo = 1

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
        )

        try:
            while True:
                fecha = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

                print()
                print(
                    f"=== Starting scan cycle #{ciclo} [{fecha}] ==="
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
                    f"=== Scan cycle complete. Next cycle in "
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
