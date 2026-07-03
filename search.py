"""
Flight Price Monitor — versão Playwright / Google Flights
"""

import re, json, os, statistics, time, random
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
import requests

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
OUTPUT_DIR       = os.environ.get("OUTPUT_DIR", "docs")

TRIP_DURATION      = 10
SEARCH_OFFSET      = 45
MIN_POINTS_FOR_PCT = 5
DELAY_BETWEEN      = (5, 8)
MIN_PRICE          = 900
MAX_PRICE          = 120_000

MONTHS_PT = ["Jan","Fev","Mar","Abr","Mai","Jun",
             "Jul","Ago","Set","Out","Nov","Dez"]
KNOWN_AIRLINES = [
    "LATAM","GOL","Azul","TAP","Iberia","Air France","KLM",
    "Lufthansa","Swiss","Turkish Airlines","EgyptAir","Emirates",
    "Qatar Airways","American Airlines","United","Delta",
    "British Airways","Copa Airlines","Aerolíneas Argentinas",
    "Sky Airline","JetSMART","ITA Airways",
]

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

def parse_brl_prices(text, min_p=MIN_PRICE):
    prices = []
    for m in re.findall(r'R\$\s*([\d]{1,3}(?:\.[\d]{3})*)', text):
        try:
            v = float(m.replace('.', ''))
            if min_p <= v <= MAX_PRICE:
                prices.append(v)
        except Exception:
            pass
    return prices

def detect_airline(text):
    tl = text.lower()
    for a in KNOWN_AIRLINES:
        if a.lower() in tl:
            return a
    return "—"

def fmt_date(d):
    if not d: return "—"
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{dt.day:02d} {MONTHS_PT[dt.month-1]}"

def build_kayak_link(o, d, dep, ret):
    return f"https://www.kayak.com.br/flights/{o}-{d}/{dep}/{ret}"

def get_status(pct, n):
    if n < MIN_POINTS_FOR_PCT: return "new"
    if pct <= -50: return "error"
    if pct <= -30: return "fire"
    if pct <= -10: return "good"
    if pct >= 20:  return "high"
    return "normal"

def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
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


# ─── Scraper ──────────────────────────────────────────────────────────────────

class GFlightsScraper:

    def __init__(self, pw):
        self.browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                  "--disable-blink-features=AutomationControlled","--lang=pt-BR"],
        )
        self.ctx = self.browser.new_context(
            locale="pt-BR", timezone_id="America/Sao_Paulo",
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self._cookies_done = False

    def _dismiss(self, page):
        if self._cookies_done: return
        for label in ["Aceitar tudo","Accept all","Concordar com tudo","Agree to all"]:
            try:
                page.get_by_role("button", name=label).click(timeout=2000)
                self._cookies_done = True
                time.sleep(0.8)
                return
            except Exception:
                pass

    def _type_and_confirm(self, page, code):
        """
        Digita um código de aeroporto no campo que estiver com foco
        e confirma com ArrowDown + Enter (mais confiável que clicar na opção).
        """
        page.keyboard.type(code, delay=110)
        time.sleep(2.2)
        page.keyboard.press("ArrowDown")
        time.sleep(0.4)
        page.keyboard.press("Enter")
        time.sleep(1.0)

    def search(self, origin, dest, dep_date, ret_date):
        page = self.ctx.new_page()
        page.set_default_timeout(20000)
        try:
            return self._search(page, origin, dest, dep_date, ret_date)
        except Exception as e:
            print(f"    [erro geral] {type(e).__name__}: {e}")
            return None, "—"
        finally:
            page.close()

    def _search(self, page, origin, dest, dep_date, ret_date):
        # ── 1. Abrir página ──────────────────────────────────────────────────
        page.goto(
            "https://www.google.com/travel/flights?hl=pt-BR&gl=BR&curr=BRL",
            wait_until="domcontentloaded", timeout=30000,
        )
        time.sleep(random.uniform(3, 4))
        self._dismiss(page)
        time.sleep(0.8)

        # ── 2. ORIGEM ────────────────────────────────────────────────────────
        # Clicar diretamente no texto "De onde?" visível na tela
        origin_clicked = False
        for text in ["De onde?", "Where from?", "De onde", "Where from"]:
            try:
                page.get_by_text(text, exact=True).first.click(timeout=4000)
                time.sleep(0.5)
                origin_clicked = True
                print(f"    campo origem clicado via texto '{text}'")
                break
            except Exception:
                pass

        if not origin_clicked:
            # Fallback: clicar na área esquerda do formulário por coordenada relativa
            try:
                page.mouse.click(350, 383)
                time.sleep(0.5)
                origin_clicked = True
                print(f"    campo origem clicado via coordenada")
            except Exception:
                pass

        if not origin_clicked:
            print(f"    [campo origem não encontrado]")
            return None, "—"

        self._type_and_confirm(page, origin)
        print(f"    origem '{origin}' digitada")

        # ── 3. DESTINO ───────────────────────────────────────────────────────
        # Após confirmar origem, Google Flights move foco para destino.
        # Tentamos clicar no texto "Para onde?" antes de digitar.
        time.sleep(0.5)
        dest_clicked = False
        for text in ["Para onde?", "Where to?", "Para onde", "Where to"]:
            try:
                page.get_by_text(text, exact=True).first.click(timeout=3000)
                time.sleep(0.5)
                dest_clicked = True
                print(f"    campo destino clicado via texto '{text}'")
                break
            except Exception:
                pass

        if not dest_clicked:
            # Fallback coordenada: lado direito do formulário
            try:
                page.mouse.click(650, 383)
                time.sleep(0.5)
                dest_clicked = True
                print(f"    campo destino clicado via coordenada")
            except Exception:
                pass

        if not dest_clicked:
            print(f"    destino via foco automático (sem clique)")

        self._type_and_confirm(page, dest)
        print(f"    destino '{dest}' digitado")

        # ── 4. DATAS ─────────────────────────────────────────────────────────
        # Baseado no screenshot: campos "Partida" e "Volta"
        dep_dt = datetime.strptime(dep_date, "%Y-%m-%d")
        ret_dt = datetime.strptime(ret_date, "%Y-%m-%d")

        # Formatos de data a tentar
        dep_fmts = [
            f"{dep_dt.day:02d}/{dep_dt.month:02d}/{dep_dt.year}",
            dep_date,
        ]
        ret_fmts = [
            f"{ret_dt.day:02d}/{ret_dt.month:02d}/{ret_dt.year}",
            ret_date,
        ]

        for fmts, keywords in [
            (dep_fmts, ["Partida","Departure","Data de partida","Check-in"]),
            (ret_fmts, ["Volta","Return","Data de volta","Check-out"]),
        ]:
            clicked = False
            for kw in keywords:
                for attr in ["placeholder", "aria-label", "aria-placeholder"]:
                    sel = f'[{attr}*="{kw}"]'
                    try:
                        el = page.locator(sel).first
                        el.wait_for(state="visible", timeout=2000)
                        el.click()
                        time.sleep(0.5)
                        clicked = True
                        # Tentar digitar a data
                        for fmt in fmts:
                            try:
                                page.keyboard.press("Control+a")
                                page.keyboard.type(fmt, delay=60)
                                time.sleep(0.4)
                                page.keyboard.press("Enter")
                                time.sleep(0.6)
                                break
                            except Exception:
                                pass
                        break
                    except Exception:
                        continue
                if clicked:
                    break

            if not clicked:
                print(f"    [data não configurada para {fmts[0]}]")

        # ── 5. PESQUISAR ─────────────────────────────────────────────────────
        searched = False
        for btn in ["Pesquisar", "Search", "Buscar"]:
            try:
                page.get_by_role("button", name=btn).click(timeout=3000)
                searched = True
                print(f"    botão '{btn}' clicado")
                break
            except Exception:
                pass
        if not searched:
            page.keyboard.press("Enter")
            print(f"    busca via Enter")

        # ── 6. AGUARDAR RESULTADOS ───────────────────────────────────────────
        print(f"    aguardando resultados...")
        time.sleep(random.uniform(6, 9))
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        time.sleep(2)

        # Screenshot de diagnóstico (primeira rota apenas)
        try:
            if not os.path.exists("debug_results.png"):
                page.screenshot(path="debug_results.png", full_page=False)
                print(f"    [debug] screenshot dos resultados salvo")
        except Exception:
            pass

        # ── 7. EXTRAIR PREÇOS ────────────────────────────────────────────────
        body_text = page.inner_text("body")

        if "captcha" in body_text.lower() or "não sou um robô" in body_text.lower():
            print("    [CAPTCHA detectado]")
            return None, "—"

        # Mostrar URL atual (diagnóstico — confirma se a busca aconteceu)
        print(f"    URL atual: {page.url[:80]}")

        # Todos os preços encontrados (diagnóstico)
        all_raw = [float(m.replace('.',''))
                   for m in re.findall(r'R\$\s*([\d]{1,3}(?:\.[\d]{3})*)', body_text)
                   if 100 <= float(m.replace('.','')) <= MAX_PRICE]
        print(f"    preços na página: {sorted(set(all_raw))[:12]}")

        prices = [v for v in all_raw if v >= MIN_PRICE]
        if not prices:
            print(f"    [sem preços acima de R$ {MIN_PRICE}]")
            return None, "—"

        best = min(prices)
        airline = detect_airline(body_text)
        return best, airline

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
    results, alerts = [], []

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

            pl   = [h["price"] for h in hist]
            n    = len(pl)
            med  = statistics.median(pl) if n > 1 else price
            pct  = round((price - med) / med * 100, 1) if n > 1 else 0.0
            stat = get_status(pct, n)
            link = build_kayak_link(route["origin"], route["dest"], dep_date, ret_date)

            result = {
                "id": rid, "from": route["from_city"], "to": route["to_city"],
                "flag": route["flag"], "origin": route["origin"], "dest": route["dest"],
                "airline": airline, "price": round(price),
                "dep_date": dep_date, "ret_date": ret_date,
                "dep_fmt": fmt_date(dep_date), "ret_fmt": fmt_date(ret_date),
                "median_30d": round(med), "pct_change": pct,
                "status": stat, "n_points": n, "link": link,
            }
            results.append(result)
            print(f"     R$ {price:,.0f} via {airline}  {pct:+.0f}%  [{stat}]")

            if stat in ("error","fire") and n >= MIN_POINTS_FOR_PCT:
                icon = "🚨 POSSÍVEL ERRO DE TARIFA" if stat == "error" else "🔥 PROMOÇÃO EXCEPCIONAL"
                alerts.append(
                    f"{icon}\n\n✈️ <b>{route['from_city']} → {route['to_city']}</b>\n\n"
                    f"💰 Preço: <b>R$ {price:,.0f}</b>\n📊 Mediana 30d: R$ {med:,.0f}\n"
                    f"📉 Variação: <b>{pct:+.0f}%</b>\n🏢 Cia: {airline}\n"
                    f"📅 Ida: {fmt_date(dep_date)}  |  Volta: {fmt_date(ret_date)}\n\n"
                    f"🔗 <a href='{link}'>Ver no Kayak</a>"
                )

            time.sleep(random.uniform(*DELAY_BETWEEN))

        scraper.close()

    results.sort(key=lambda r: r["pct_change"] if r["status"] != "new" else 999)
    payload = {
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "updated_fmt": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "total": len(results), "routes": results,
    }
    save_json(data_path, payload)
    save_json(history_path, history)
    print(f"✓ {len(results)} rotas salvas em {OUTPUT_DIR}/")

    for msg in alerts:
        send_telegram(msg)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
