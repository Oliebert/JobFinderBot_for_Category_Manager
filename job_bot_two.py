import logging
import re
import time
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

from config import TOKEN, CHAT_ID

KEYWORDS = [
    'Category Management', 'Category Manager', 'Einkauf', 'Einkäufer',
    'Purchasing', 'Beschaffung', 'Sales Manager', 'E-Commerce',
    'Non Food', 'Vendor Manager', 'Head of E-Commerce', 'Head of Category Management' 'Leitung','Leiter'
]
EXCLUDE = [
    'marketing', 'praktikum', 'werkstudent', 'ausbildung',
    'sachbearbeiter', 'trainee', 'junior', 'mitarbeiter', 'sachbearbeitung', 'immobilien',
'facility','assistent','assistenz']

CENTER_CITY = "Bietigheim-Bissingen"
AA_RADIUS_KM = 35
STEPSTONE_RADIUS_KM = 30

AA_ENDPOINTS = [
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v5/jobs",
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/app/jobs",
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs",
]

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

AA_HEADERS = {
    "X-API-Key": "jobboerse-jobsuche",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Origin": "https://www.arbeitsagentur.de",
    "Referer": "https://www.arbeitsagentur.de/jobsuche/",
    "User-Agent": BROWSER_USER_AGENT,
}


def to_stepstone_slug(text: str) -> str:
    """Wandelt Suchbegriffe und Ortsnamen in StepStone-kompatible URL-Slugs um."""
    slug = text.lower().strip()
    for src, dst in [("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")]:
        slug = slug.replace(src, dst)
    return re.sub(r"[^a-z0-9]+", "-", slug).strip("-")


def detect_working_aa_endpoint() -> str | None:
    """Prüft, ob ein REST-Endpunkt der Arbeitsagentur direkt antwortet."""
    test_params = {"was": "Einkauf", "wo": "Stuttgart", "umkreis": 25, "page": 1, "size": 1}
    for url in AA_ENDPOINTS:
        try:
            resp = requests.get(url, headers=AA_HEADERS, params=test_params, timeout=8)
            if resp.status_code == 200:
                print(f"[AA API] Aktiver Endpunkt: {url}")
                return url
        except Exception:
            pass
    return None


def search_arbeitsagentur_api(endpoint: str, keyword: str, location: str, radius: int) -> list[dict]:
    params = {
        "was": keyword,
        "wo": location,
        "umkreis": radius,
        "page": 1,
        "size": 25,
        "angebotsart": 1,
    }
    try:
        response = requests.get(endpoint, headers=AA_HEADERS, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"[AA API ERROR] '{keyword}': {e}")
        return []

    jobs = []
    for item in data.get("stellenangebote", []):
        refnr = item.get("refnr", "")
        titel = item.get("titel", "Ohne Titel")
        arbeitgeber = item.get("arbeitgeber", "Unbekannt")
        ort = item.get("arbeitsort", {}).get("ort", "")
        link = f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}" if refnr else ""

        jobs.append({
            "id": f"aa_{refnr}",
            "source": "Arbeitsagentur",
            "title": titel,
            "company": arbeitgeber,
            "location": ort,
            "link": link,
        })

    print(f"[AA API DEBUG] '{keyword}': {len(jobs)} Treffer")
    return jobs


async def scrape_arbeitsagentur_page(page, keywords: list[str], location: str, radius: int) -> list[dict]:
    """Durchsucht die Arbeitsagentur über den Browser (Fallback)."""
    jobs = []
    for kw in keywords:
        url = (
            f"https://www.arbeitsagentur.de/jobsuche/suche"
            f"?was={requests.utils.quote(kw)}&wo={requests.utils.quote(location)}&umkreis={radius}&angebotsart=1"
        )
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(2500)
            links = await page.locator("a[href*='/jobsuche/jobdetail/']").all()
            kw_count = 0
            for link_el in links:
                href = await link_el.get_attribute("href") or ""
                text = (await link_el.inner_text()).strip()
                if not href or not text:
                    continue
                if not href.startswith("http"):
                    href = "https://www.arbeitsagentur.de" + href
                clean_href = href.split("?")[0]
                refnr = clean_href.rstrip("/").split("/")[-1]
                title = text.splitlines()[0].strip()
                jobs.append({
                    "id": f"aa_{refnr}",
                    "source": "Arbeitsagentur",
                    "title": title,
                    "company": "Arbeitsagentur",
                    "location": location,
                    "link": clean_href,
                })
                kw_count += 1
            print(f"[AA Browser DEBUG] '{kw}': {kw_count} Treffer")
        except Exception as e:
            print(f"[AA Browser ERROR] '{kw}': {e}")
    return jobs


async def scrape_stepstone_page(page, keywords: list[str], location: str, radius: int) -> tuple[list[dict], bool]:
    """Durchsucht StepStone im angegebenen Umkreis per Playwright. Gibt (Treffer, geblockt) zurück."""
    jobs = []
    loc_slug = to_stepstone_slug(location)
    consent_handled = False
    blocked = False

    for idx, kw in enumerate(keywords):
        kw_slug = to_stepstone_slug(kw)
        url = f"https://www.stepstone.de/jobs/{kw_slug}/in-{loc_slug}?radius={radius}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(2500)

            if not consent_handled:
                for selector in [
                    "#ccmgt_explicit_accept",
                    "button:has-text('Alle akzeptieren')",
                    "button:has-text('Akzeptieren')",
                ]:
                    btn = page.locator(selector).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(1200)
                        break
                consent_handled = True

            page_title = await page.title()
            if any(word in page_title.lower() for word in ["access denied", "attention required", "just a moment", "forbidden"]):
                print(f"[StepStone BLOCKED] Seite blockiert: '{page_title}'")
                blocked = True
                break

            articles = await page.locator("article[data-at='job-item'], article").all()
            kw_count = 0

            for art in articles:
                title_el = art.locator("[data-at='job-item-title'], h2 a, h2").first
                if await title_el.count() == 0:
                    continue

                title = (await title_el.inner_text()).strip()
                if not title:
                    continue

                href = await title_el.get_attribute("href")
                if not href:
                    link_el = art.locator("a[href*='/stellenangebote'], a[href*='/job/'], a[href]").first
                    if await link_el.count() > 0:
                        href = await link_el.get_attribute("href")

                if not href:
                    continue
                if not href.startswith("http"):
                    href = "https://www.stepstone.de" + href
                clean_link = href.split("?")[0]

                company_el = art.locator("[data-at='job-item-company-name']").first
                loc_el = art.locator("[data-at='job-item-location']").first

                company = (await company_el.inner_text()).strip() if await company_el.count() > 0 else "StepStone"
                job_loc = (await loc_el.inner_text()).strip() if await loc_el.count() > 0 else f"Umkreis {location}"

                jobs.append({
                    "id": f"st_{clean_link}",
                    "source": "StepStone",
                    "title": title,
                    "company": company,
                    "location": job_loc,
                    "link": clean_link,
                })
                kw_count += 1

            print(f"[StepStone DEBUG] '{kw}' ({radius} km) | Titel: '{page_title[:45]}' | Treffer: {kw_count}")

            if idx == 0 and len(articles) == 0:
                print("[StepStone WARN] 0 Artikel im Headless-Modus geladen – wechsle auf sichtbares Browserfenster...")
                blocked = True
                break

        except Exception as e:
            print(f"[StepStone ERROR] '{kw}': {e}")

    return jobs, blocked


async def run_browser_searches(
    use_aa_browser: bool,
    keywords: list[str],
    location: str,
    aa_radius: int,
    stepstone_radius: int,
) -> tuple[list[dict], bool]:
    """Startet Chromium (zuerst unsichtbar, bei StepStone-Blockade automatisch sichtbar)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return [], False

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--headless=new", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=BROWSER_USER_AGENT,
            locale="de-DE",
            viewport={"width": 1366, "height": 900},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        if use_aa_browser:
            results.extend(await scrape_arbeitsagentur_page(page, keywords, location, aa_radius))

        st_jobs, st_blocked = await scrape_stepstone_page(page, keywords, location, stepstone_radius)
        await browser.close()

        if st_blocked:
            browser_vis = await p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context_vis = await browser_vis.new_context(
                user_agent=BROWSER_USER_AGENT,
                locale="de-DE",
                viewport={"width": 1366, "height": 900},
            )
            await context_vis.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page_vis = await context_vis.new_page()
            st_jobs, _ = await scrape_stepstone_page(page_vis, keywords, location, stepstone_radius)
            await browser_vis.close()

        results.extend(st_jobs)

    return results, True


def filter_and_mix_results(results: list[dict], max_per_source: int = 15) -> tuple[list[dict], int, int]:
    """Entfernt Duplikate/Ausschlusswörter und mischt StepStone und Arbeitsagentur gleichmäßig."""
    seen_ids = set()
    st_jobs = []
    aa_jobs = []

    for job in results:
        job_id = job.get("id") or job.get("link")
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        title_lower = job["title"].lower()
        if any(word.lower() in title_lower for word in EXCLUDE):
            continue

        if job["source"] == "StepStone":
            st_jobs.append(job)
        else:
            aa_jobs.append(job)

    mixed = []
    st_slice = st_jobs[:max_per_source]
    aa_slice = aa_jobs[:max_per_source]
    for i in range(max(len(st_slice), len(aa_slice))):
        if i < len(st_slice):
            mixed.append(st_slice[i])
        if i < len(aa_slice):
            mixed.append(aa_slice[i])

    return mixed, len(st_jobs), len(aa_jobs)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hallo! Dein Job-Bot (v3: StepStone + Arbeitsagentur gemischt) ist aktiv.\n"
        f"Sende /search, um Stellenangebote auf StepStone ({STEPSTONE_RADIUS_KM} km) "
        f"und Arbeitsagentur ({AA_RADIUS_KM} km) rund um {CENTER_CITY} abzurufen."
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🔍 Suche läuft auf StepStone ({STEPSTONE_RADIUS_KM} km) und Arbeitsagentur ({AA_RADIUS_KM} km)... "
        "Bitte ca. 35–50 Sekunden warten."
    )
    raw_results = []

    aa_endpoint = detect_working_aa_endpoint()
    use_aa_browser = aa_endpoint is None

    if aa_endpoint:
        for kw in KEYWORDS:
            raw_results.extend(search_arbeitsagentur_api(aa_endpoint, kw, CENTER_CITY, AA_RADIUS_KM))
            time.sleep(0.25)

    browser_results, playwright_ok = await run_browser_searches(
        use_aa_browser=use_aa_browser,
        keywords=KEYWORDS,
        location=CENTER_CITY,
        aa_radius=AA_RADIUS_KM,
        stepstone_radius=STEPSTONE_RADIUS_KM,
    )

    if not playwright_ok:
        await update.message.reply_text(
            "⚠️ Playwright ist noch nicht installiert. Bitte im PyCharm-Terminal ausführen:\n"
            "pip install playwright\n"
            "playwright install chromium"
        )
    else:
        raw_results.extend(browser_results)

    mixed_jobs, total_st, total_aa = filter_and_mix_results(raw_results, max_per_source=15)

    if not mixed_jobs:
        await update.message.reply_text("Keine passenden Stellenangebote gefunden.")
        return

    await update.message.reply_text(
        f"✅ Gefunden: {total_st + total_aa} Stellen (StepStone: {total_st} | Arbeitsagentur: {total_aa})\n"
        f"Zeige bis zu 15 Treffer je Portal (abwechselnd sortiert):"
    )

    for i in range(0, len(mixed_jobs), 5):
        batch = mixed_jobs[i:i + 5]
        lines = []
        for job in batch:
            lines.append(
                f"📌 {job['title']}\n"
                f"🏢 {job['company']} ({job['location']})\n"
                f"🌐 {job['source']}\n"
                f"🔗 {job['link']}"
            )
        await update.message.reply_text("\n\n".join(lines), disable_web_page_preview=True)


if __name__ == '__main__':
    print("Bot (v3: StepStone + Arbeitsagentur gemischt) startet und wartet auf Befehle in Telegram...")
    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(20.0)
        .read_timeout(20.0)
        .write_timeout(20.0)
        .pool_timeout(20.0)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search))
    app.run_polling(bootstrap_retries=5)