"""
Flight Price Monitor — versão Playwright / Google Flights
"""

import re
import json
import os
import statistics
import time
import random
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
import requests

# ─── Credenciais ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
OUTPUT_DIR       = os.environ.get("OUTPUT_DIR", "docs")

# ─── Configuração ─────────────────────────────────────────────────────────────
TRIP_DURATION      = 10
SEARCH_OFFSET      = 45
MIN_POINTS_FOR_PCT = 5
DELAY_BETWEEN      = (8, 14)

MONTHS_PT = ["Jan","Fev","Mar","Abr","Mai","Jun",
             "Jul","Ago","Set","Out","Nov","Dez"]
MONTHS_LONG = {
    1:"janeiro",2:"fevereiro",3:"março",4:"abril",
    5:"maio",6:"junho",7:"julho",8:"agosto",
    9:"setembro",10:"outubro",11:"novembro",12:"dezembro"
}
KNOWN_AIRLINES = [
    "LATAM","GOL","Azul","TAP","Iberia","Air France","KLM",
    "Lufthansa","Swiss","Turkish Airlines","EgyptAir","Emirates",
    "Qatar Airways","American Airlines","United","Delta",
    "British Airways","Copa Airlines","Aerolíneas Argentinas",
    "Sky Airline","JetSMART","ITA Airways",
]

# ─── Rotas ────────────────────────────────────────────────────────────────────
ROUTES = [
    {"id":"GRU-CUN","origin":"GRU","dest":"CUN","from_city":"São Paulo",    "to_city":"Cancún",       "flag":"🇲🇽"},
    {"id":"GRU-MIA","origin":"GRU","dest":"MIA","from_city":"São Paulo",    "to_city":"Miami",        "flag":"🇺🇸"},
    {"id":"GRU-LIS","origin":"GRU","dest":"LIS","from_city":"São Paulo",    "to_city":"Lisboa",       "flag":"🇵🇹"},
    {"id":"GRU-MAD","origin":"GRU","dest":"MAD","from_city":"São Paulo",    "to_city":"Madri",        "flag":"🇪🇸"},
    {"id":"GRU-CDG","origin":"GRU","dest":"CDG","from_city":"São Paulo",    "to_city":"Paris",        "flag":"🇫🇷"},
    {"id":"GRU-FCO","origin":"GRU","dest":"FCO","from_city":"São Paulo",    "to_city":"Roma",         "flag":"🇮🇹"},
    {"id":"GRU-GVA","origin":"GRU","dest":"GVA","from_city":"São Paulo",    "to_city":"Genebra",      "flag":"🇨🇭"},
    {"id":"GRU-IST","origin":"GRU","dest":"IST","from_city":"São Paulo",    "to_city":"Istambul",     "flag":"🇹🇷"},
    {"id":"GRU-CAI","origin":"GRU","dest":"CAI","from_city":"São Paulo",    "to_city":"Cairo",        "flag":"🇪🇬"},
    {"id":"GRU-DXB","origin":"GRU","dest":"DXB","from_city":"São Paulo",    "to_city":"Dubai",        "flag":"🇦🇪"},
    {"id":"CWB-SCL","origin":"CWB","dest":"SCL","from_city":"Curitiba",     "to_city":"Santiago",     "flag":"🇨🇱"},
    {"id":"CWB-EZE","origin":"CWB","dest":"EZE","from_city":"Curitiba",     "to_city":"Buenos Aires", "flag":"🇦🇷"},
    {"id":"CWB-BRC","origin":"CWB","dest":"BRC","from_city":"Curitiba",     "to_city":"Bariloche",    "flag":"🇦🇷"},
    {"id":"FLN-EZE","origin":"FLN","dest":"EZE","from_city":"Florianópolis","to_city":"Buenos Aires", "flag":"🇦🇷"},
    {"id":"FLN-BRC","origin":"FLN","dest":"BRC","from_city":"Florianópolis","to_city":"Bariloche",    "flag":"🇦🇷"},
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_brl_prices(text):
    matches = re.findall(r'R\$\s*([\d]{1,3}(?:\.[\d]{3})*)', text)
    prices = []
    for m in matches:
        try:
            val = float(m.replace('.', ''))
            if 200 < val < 120_000:
                prices.append(val)
        except Exception:
            pass
    return prices

def detect_airline(text):
    text_lower = text.lower()
    for a in KNOWN_AIRLINES:
        if a.lower() in text_lower:
            return a
    return "—"

def fmt_date(d):
    if not d: return "—"
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{dt.day:02d} {MONTHS_PT[dt.month-1]}"

def build_kayak_link(origin, dest, dep, ret):
    return f"https://www.kayak.com.br/flights/{origin}-{dest}/{dep}/{ret}"

def get_status(pct, n):
    if n < MIN_POINTS_FOR_PCT: return "new"
    if pct <= -50: return "error"
    if pct <= -30: return "fire"
    if pct <= -10: return "good"
    if pct >= 20:  return "high"
    return "normal"

def load_json(filepath, default=None):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}

def save_json(filepath, data):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        )
    except Exception as e:
        print(f"  [Telegram] {e}")


# ─── Google Flights scraper ───────────────────────────────────────────────────

class GFlightsScraper:

    def __init__(self, pw):
        self.browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--lang=pt-BR",
            ],
        )
        self.ctx = self.browser.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self._cookies_done = False
        self._debug_done   = False   # salva screenshot só da primeira rota

    def _accept_cookies(self, page):
        if self._cookies_done:
            return
        for label in ["Aceitar tudo", "Accept all", "Concordar com tudo",
                       "Tout accepter", "Agree to all", "I agree"]:
            try:
                page.get_by_role("button", name=label).click(timeout=2500)
                self._cookies_done = True
                time.sleep(1)
                return
            except Exception:
                pass

    def _try_fill(self, page, code):
        """
        Tenta preencher um campo de aeroporto de várias formas.
        Retorna True se conseguiu confirmar uma opção no autocomplete.
        """
        # Lista de seletores a tentar — do mais específico ao mais genérico
        selectors = [
            # Por placeholder (mais estável)
            'input[placeholder*="De onde"]',
            'input[placeholder*="Where from"]',
            'input[placeholder*="Para onde"]',
            'input[placeholder*="Where to"]',
            'input[placeholder*="Origem"]',
            'input[placeholder*="Destino"]',
            # Por aria-label
            'input[aria-label*="De onde"]',
            'input[aria-label*="Where from"]',
            'input[aria-label*="Para onde"]',
            'input[aria-label*="Where to"]',
            # Genérico: qualquer input de texto visível
            'input[type="text"]:visible',
        ]

        for sel in selectors:
            try:
                els = page.locator(sel).all()
                for el in els:
                    try:
                        el.click(timeout=2000)
                        time.sleep(0.4)
                        el.press("Control+a")
                        el.type(code, delay=90)
                        time.sleep(2.0)
                        opts = page.get_by_role("option").all()
                        if opts:
                            opts[0].click()
                            time.sleep(0.7)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def search(self, origin, dest, dep_date, ret_date):
        page = self.ctx.new_page()
        page.set_default_timeout(20000)
        try:
            return self._search(page, origin, dest, dep_date, ret_date)
        except Exception as e:
            print(f"    [erro geral] {e}")
            return None, "—"
        finally:
            page.close()

    def _search(self, page, origin, dest, dep_date, ret_date):
        page.goto(
            "https://www.google.com/travel/flights?hl=pt-BR&gl=BR&curr=BRL",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        time.sleep(random.uniform(3, 5))
        self._accept_cookies(page)
        time.sleep(1)

        # Screenshot de diagnóstico (só na primeira rota)
        if not self._debug_done:
            try:
                page.screenshot(path="debug_page.png", full_page=True)
                print("    [debug] screenshot salvo em debug_page.png")
            except Exception as e:
                print(f"    [debug screenshot erro] {e}")
            self._debug_done = True

        # Imprimir texto da página para diagnóstico
        try:
            body = page.inner_text("body")
            preview = body[:300].replace("\n", " ")
            print(f"    [página] {preview}...")
        except Exception:
            pass

        # Preencher origem
        print(f"    preenchendo origem: {origin}")
        if not self._try_fill(page, origin):
            print(f"    [origem falhou]")
            return None, "—"

        # Preencher destino
        print(f"    preenchendo destino: {dest}")
        if not self._try_fill(page, dest):
            print(f"    [destino falhou]")
            return None, "—"

        # Datas — tenta preencher via aria-label
        dep_dt = datetime.strptime(dep_date, "%Y-%m-%d")
        ret_dt = datetime.strptime(ret_date, "%Y-%m-%d")

        for date_str, keywords in [
            (dep_date, ["Partida","Departure","Data de partida","Data ida"]),
            (ret_date, ["Volta","Return","Data de volta","Data volta"]),
        ]:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            formats = [
                date_str,
                f"{dt.day}/{dt.month}/{dt.year}",
                f"{dt.day:02d}/{dt.month:02d}/{dt.year}",
            ]
            for kw in keywords:
                for sel in [f'input[aria-label*="{kw}"]', f'[aria-label*="{kw}"]']:
                    try:
                        el = page.locator(sel).first
                        el.wait_for(timeout=2000)
                        for fmt in formats:
                            try:
                                el.click(timeout=2000)
                                el.press("Control+a")
                                el.type(fmt, delay=60)
                                el.press("Enter")
                                time.sleep(0.8)
                                break
                            except Exception:
                                pass
                        break
                    except Exception:
                        pass

        # Pesquisar
        for btn in ["Pesquisar", "Search", "Buscar"]:
            try:
                page.get_by_role("button", name=btn).click(timeout=3000)
                break
            except Exception:
                pass
        else:
            page.keyboard.press("Enter")

        time.sleep(random.uniform(7, 11))
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(2)

        body_text = page.inner_text("body")
        if "captcha" in body_text.lower() or "não sou um robô" in body_text.lower():
            print("    [CAPTCHA detectado]")
            return None, "—"

        prices = parse_brl_prices(body_text)
        if not prices:
            print("    [nenhum preço encontrado]")
            return None, "—"

        return min(prices), detect_airline(body_text)

    def close(self):
        self.browser.close()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"▶ Flight Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC")

    today     = datetime.now().date()
    dep_date  = (today + timedelta(days=SEARCH_OFFSET)).strftime("%Y-%m-%d")
    ret_date  = (today + timedelta(days=SEARCH_OFFSET + TRIP_DURATION)).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")
    cutoff    = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    print(f"  Ida: {dep_date}  |  Volta: {ret_date}")

    history_path = os.path.join(OUTPUT_DIR, "history.json")
    data_path    = os.path.join(OUTPUT_DIR, "data.json")
    history = load_json(history_path, default={})

    results = []
    alerts  = []

    with sync_playwright() as pw:
        scraper = GFlightsScraper(pw)

        for route in ROUTES:
            print(f"  → {route['id']}  {route['from_city']} → {route['to_city']}")
            price, airline = scraper.search(route["origin"], route["dest"], dep_date, ret_date)

            if price is None:
                print(f"     sem resultado")
                time.sleep(random.uniform(*DELAY_BETWEEN))
                continue

            rid  = route["id"]
            hist = history.get(rid, [])
            hist = [h for h in hist if h["date"] != today_str and h["date"] >= cutoff]
            hist.append({"date": today_str, "price": price})
            history[rid] = hist

            prices_list = [h["price"] for h in hist]
            n      = len(prices_list)
            median = statistics.median(prices_list) if n > 1 else price
            pct    = round((price - median) / median * 100, 1) if n > 1 else 0.0
            status = get_status(pct, n)
            link   = build_kayak_link(route["origin"], route["dest"], dep_date, ret_date)

            result = {
                "id": rid, "from": route["from_city"], "to": route["to_city"],
                "flag": route["flag"], "origin": route["origin"], "dest": route["dest"],
                "airline": airline, "price": round(price),
                "dep_date": dep_date, "ret_date": ret_date,
                "dep_fmt": fmt_date(dep_date), "ret_fmt": fmt_date(ret_date),
                "median_30d": round(median), "pct_change": pct,
                "status": status, "n_points": n, "link": link,
            }
            results.append(result)
            print(f"     R$ {price:,.0f} via {airline}  {pct:+.0f}%  [{status}]")

            if status in ("error", "fire") and n >= MIN_POINTS_FOR_PCT:
                icon = "🚨 POSSÍVEL ERRO DE TARIFA" if status == "error" else "🔥 PROMOÇÃO EXCEPCIONAL"
                alerts.append(
                    f"{icon}\n\n✈️ <b>{route['from_city']} → {route['to_city']}</b>\n\n"
                    f"💰 Preço: <b>R$ {price:,.0f}</b>\n📊 Mediana 30d: R$ {median:,.0f}\n"
                    f"📉 Variação: <b>{pct:+.0f}%</b>\n🏢 Cia: {airline}\n"
                    f"📅 Ida: {fmt_date(dep_date)}  |  Volta: {fmt_date(ret_date)}\n\n"
                    f"🔗 <a href='{link}'>Ver no Kayak</a>"
                )

            time.sleep(random.uniform(*DELAY_BETWEEN))

        scraper.close()

    results.sort(key=lambda r: r["pct_change"] if r["status"] != "new" else 999)
    payload = {
        "updated_at":  datetime.utcnow().isoformat() + "Z",
        "updated_fmt": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "total":       len(results),
        "routes":      results,
    }

    save_json(data_path,    payload)
    save_json(history_path, history)
    print(f"✓ {len(results)} rotas salvas em {OUTPUT_DIR}/")

    for msg in alerts:
        send_telegram(msg)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
