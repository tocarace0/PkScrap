import asyncio
import json
import os
import random
import re
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urljoin

from playwright.async_api import async_playwright


# ============================================================
# CONFIGURATION
# ============================================================

# Your Discord webhook is read from this environment variable.
DISCORD_WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"

# This file remembers products already successfully sent to Discord.
# Delete it if you want to receive alerts again for existing products.
NOTIFIED_PRODUCTS_FILE = "productos_notificados_english.json"

# True = only detect Pokémon 30th Anniversary products.
# False = detect all English Pokémon products found in the configured searches.
REQUIRE_30TH_ANNIVERSARY = True

# Maximum time allowed for one website to load.
PAGE_TIMEOUT_MS = 20000

# Wait after each website loads before reading its products.
# Lower values are faster but may miss products still loading.
MIN_WAIT_BETWEEN_STORES_MS = 1200
MAX_WAIT_BETWEEN_STORES_MS = 2200

# Time between complete scans of all stores.
MIN_WAIT_BETWEEN_CYCLES_SECONDS = 90
MAX_WAIT_BETWEEN_CYCLES_SECONDS = 150

# Maximum number of previously reported links stored locally.
MAX_REPORTED_LINKS = 3000


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
# TEXT AND FILE HELPERS
# ============================================================

def normalizar_texto(texto):
    """Lowercases text and removes accents."""

    texto_normalizado = unicodedata.normalize("NFD", texto.lower())

    return "".join(
        caracter
        for caracter in texto_normalizado
        if unicodedata.category(caracter) != "Mn"
    )


def texto_error_corto(error, limite=180):
    """Returns a shorter one-line error message."""

    texto = str(error).replace("\n", " ").strip()

    if len(texto) > limite:
        return texto[:limite] + "..."

    return texto or "Error desconocido"


def es_producto_ingles(texto):
    """
    Returns True only when a product listing explicitly indicates
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
    Returns True when text appears to refer to Pokémon's
    30th Anniversary collection.
    """

    texto_normalizado = normalizar_texto(texto)

    indicadores_30_aniversario = (
        "30 aniversario",
        "aniversario 30",
        "30th anniversary",
        "30th",
        "coleccion 30",
        "collection 30",
    )

    indicadores_falsos = (
        "20th",
        "25th",
        "20 aniversario",
        "25 aniversario",
        "30 cm",
        "30cm",
    )

    tiene_indicador = any(
        indicador in texto_normalizado
        for indicador in indicadores_30_aniversario
    )

    tiene_falso_positivo = any(
        indicador in texto_normalizado
        for indicador in indicadores_falsos
    )

    return tiene_indicador and not tiene_falso_positivo


def cargar_links_reportados():
    """Loads links already successfully sent to Discord."""

    try:
        if not os.path.exists(NOTIFIED_PRODUCTS_FILE):
            return set()

        with open(
            NOTIFIED_PRODUCTS_FILE,
            "r",
            encoding="utf-8",
        ) as archivo:
            datos = json.load(archivo)

        if isinstance(datos, list):
            return {
                enlace
                for enlace in datos
                if isinstance(enlace, str)
            }

    except (OSError, json.JSONDecodeError) as error:
        print(
            f"[WARNING] No se pudo leer "
            f"{NOTIFIED_PRODUCTS_FILE}: {error}"
        )

    return set()


def guardar_links_reportados(links_reportados):
    """
    Saves notified links only after Discord has successfully
    accepted the notification.
    """

    try:
        links_ordenados = sorted(links_reportados)

        if len(links_ordenados) > MAX_REPORTED_LINKS:
            links_ordenados = links_ordenados[-MAX_REPORTED_LINKS:]

        with open(
            NOTIFIED_PRODUCTS_FILE,
            "w",
            encoding="utf-8",
        ) as archivo:
            json.dump(
                links_ordenados,
                archivo,
                ensure_ascii=False,
                indent=2,
            )

    except OSError as error:
        print(
            f"[WARNING] No se pudo guardar "
            f"{NOTIFIED_PRODUCTS_FILE}: {error}"
        )


# ============================================================
# DISCORD NOTIFICATION
# ============================================================

async def enviar_discord(webhook_url, tienda, productos):
    """
    Sends matching English products to Discord.

    Returns True only when Discord accepts every message.
    """

    bloques = []

    for producto in productos:
        bloques.append(
            f"**{producto['titulo']}**\n"
            f"Precio: {producto['precio']}\n"
            f"Idioma: English\n"
            f"Link: {producto['link']}"
        )

    encabezado = (
        f"**ENGLISH POKÉMON PRODUCT DETECTED — {tienda}**\n"
        f"New products: {len(productos)}\n\n"
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
                "User-Agent": "PokemonEnglishStockMonitor/1.0",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=20) as respuesta:
            return respuesta.status

    for contenido in mensajes:
        payload = {
            "username": "English Pokemon Stock Alert",
            "content": contenido[:1900],
            "allowed_mentions": {
                "parse": [],
            },
        }

        try:
            estado = await asyncio.to_thread(publicar, payload)

            if estado not in (200, 204):
                print(
                    f"[ERROR] Discord respondió con estado inesperado: "
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
                f"[ERROR] No se pudo enviar Discord: "
                f"{texto_error_corto(error)}"
            )
            return False

    return True


# ============================================================
# PRODUCT EXTRACTION
# ============================================================

def obtener_titulo(lineas):
    """Attempts to choose the best product title from an anchor."""

    for linea in lineas:
        linea_normalizada = normalizar_texto(linea)

        if "s/" in linea.lower():
            continue

        if "precio" in linea_normalizada:
            continue

        if len(linea.strip()) >= 8:
            return linea.strip()

    return lineas[0].strip()


def obtener_precio(lineas):
    """Attempts to find a Peruvian Sol price in the anchor text."""

    for linea in lineas:
        if "s/" in linea.lower():
            return linea.strip()

    for linea in lineas:
        if "precio" in normalizar_texto(linea):
            return linea.strip()

    return "Precio no disponible"


async def buscar_productos_ingles(page, tienda, datos):
    """
    Scans one store and returns products that match:
    - Pokémon;
    - English / Inglés;
    - optionally Pokémon 30th Anniversary.
    """

    url = datos["url"]
    base_url = datos["base"]

    await page.goto(
        url,
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
                    text.length < 350 &&
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
    links_vistos = set()
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

        link = urljoin(base_url, href)

        if link in links_vistos:
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

        links_vistos.add(link)
        titulos_vistos.add(clave_titulo)

        productos.append({
            "tienda": tienda,
            "titulo": titulo,
            "precio": precio,
            "link": link,
        })

    return productos


# ============================================================
# SCANNING LOOP
# ============================================================

async def ejecutar_ciclo(browser, links_reportados, webhook_url):
    """Scans every configured store once."""

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
                productos = await buscar_productos_ingles(
                    page,
                    tienda,
                    datos,
                )

                if not productos:
                    print(f"{tienda}: No English matching products.")
                    continue

                productos_nuevos = [
                    producto
                    for producto in productos
                    if producto["link"] not in links_reportados
                ]

                if not productos_nuevos:
                    print(
                        f"{tienda}: Found {len(productos)} English product(s), "
                        "but they were already reported."
                    )
                    continue

                print(
                    f"{tienda}: Found {len(productos_nuevos)} new "
                    "English product(s). Sending Discord alert..."
                )

                discord_enviado = await enviar_discord(
                    webhook_url,
                    tienda,
                    productos_nuevos,
                )

                if discord_enviado:
                    for producto in productos_nuevos:
                        links_reportados.add(producto["link"])

                    guardar_links_reportados(links_reportados)

                    print(
                        f"{tienda}: Discord alert sent successfully."
                    )
                else:
                    print(
                        f"{tienda}: Discord notification failed. "
                        "Products will be retried next cycle."
                    )

            except Exception as error:
                print(
                    f"{tienda}: Scan error: "
                    f"{texto_error_corto(error)}"
                )

    finally:
        await context.close()


async def main():
    """Runs the monitor continuously until Ctrl+C is pressed."""

    webhook_url = os.getenv(DISCORD_WEBHOOK_ENV_VAR)

    if not webhook_url:
        print(
            f"[ERROR] Discord is not configured. "
            f"Set {DISCORD_WEBHOOK_ENV_VAR} before starting the script."
        )
        return

    if not webhook_url.startswith("https://discord.com/api/webhooks/"):
        print(
            "[WARNING] The Discord webhook URL does not look valid. "
            "Verify DISCORD_WEBHOOK_URL."
        )

    links_reportados = cargar_links_reportados()

    print("=" * 60)
    print("ENGLISH POKEMON DISCORD MONITOR STARTED")
    print(f"Stores configured: {len(TIENDAS)}")
    print(f"Previously reported links: {len(links_reportados)}")
    print(
        "30th Anniversary filter: "
        f"{'ON' if REQUIRE_30TH_ANNIVERSARY else 'OFF'}"
    )
    print("Press Ctrl+C to stop.")
    print("=" * 60)

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
                    links_reportados,
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
