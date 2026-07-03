"""
Flight Price Monitor — versão Playwright / Google Flights
Substitui a Amadeus API (descontinuada em jul/2026).
Abre um Chrome headless, pesquisa cada rota no Google Flights,
extrai o menor preço, mantém histórico e dispara alertas no Telegram.
"""

import re
import json
import os
import io
import ftplib
import statistics
import time
import random
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
import requests

# ─── Credenciais (GitHub Secrets) ────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FTP_HOST         = os.environ["FTP_HOST"]
FTP_USER         = os.environ["FTP_USER"]
FTP_PASS         = os.environ["FTP_PASS"]
FTP_PATH         = os.environ.get("FTP_PATH", "/public_html/monitor-de-voos")

# ─── Configuração ─────────────────────────────────────────────────────────────
TRIP_DURATION      = 10   # dias de estadia padrão
SEARCH_OFFSET      = 45   # busca voos com X dias de antecedência
MIN_POINTS_FOR_PCT = 5    # dias mínimos de histórico para calcular variação
DELAY_BETWEEN      = (8, 14)  # pausa aleatória entre rotas (segundos)

MONTHS_PT = ["Jan","Fev","Mar","Abr","Mai","Jun",
             "Jul","Ago","Set","Out","Nov","Dez"]

MONTHS_LONG = {
    1:"janeiro", 2:"fevereiro", 3:"março", 4:"abril",
    5:"maio", 6:"junho", 7:"julho", 8:"agosto",
    9:"setembro", 10:"outubro", 11:"novembro", 12:"dezembro"
}

# ─── Companhias aéreas (para detectar no texto da página) ────────────────────
KNOWN_AIRLINES = [
    "LATAM", "GOL", "Azul", "TAP", "Iberia", "Air France", "KLM",
    "Lufthansa", "Swiss", "Turkish Airlines", "EgyptAir", "Emirates",
    "Qatar Airways", "American Airlines", "United", "Delta",
    "British Airways", "Copa Airlines", "Aerolíneas Argentinas",
    "Sky Airline", "JetSMART", "ITA Airways",
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


# ─── Helpers de preço ─────────────────────────────────────────────────────────

def parse_brl_prices(text):
    """
    Extrai valores monetários em BRL do texto de uma página.
    Formato brasileiro: R$ 2.840 (ponto = separador de milhar).
    """
    matches = re.findall(r'R\$\s*([\d]{1,3}(?:\.[\d]{3})*)', text)
    prices = []
    for m in matches:
        try:
            val = float(m.replace('.', ''))
            if 200 < val < 120_000:   # faixa realista de passagens
                prices.append(val)
        except Exception:
            pass
    return prices


def detect_airline(text):
    """Retorna o nome da primeira cia aérea encontrada no texto."""
    text_lower = text.lower()
    for a in KNOWN_AIRLINES:
        if a.lower() in text_lower:
            return a
    return "—"


# ─── Helpers de data/formatação ───────────────────────────────────────────────

def fmt_date(d):
    if not d:
        return "—"
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


# ─── FTP ─────────────────────────────────────────────────────────────────────

def ftp_download_json(ftp, filename, default=None):
    try:
        buf = io.BytesIO()
        ftp.retrbinary(f"RETR {filename}", buf.write)
        buf.seek(0)
        return json.loads(buf.read().decode("utf-8"))
    except Exception:
        return default if default is not None else {}


def ftp_upload_json(ftp, data, filename):
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    ftp.storbinary(f"STOR {filename}", io.BytesIO(content))


# ─── Telegram ────────────────────────────────────────────────────────────────

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
    """
    Controla um Chrome headless para pesquisar preços no Google Flights.
    Um browser é aberto por execução completa e reutilizado entre rotas.
    """

    def __init__(self, pw):
        self.browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
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

    # ── Utilitários internos ───────────────────────────────────────────────

    def _accept_cookies(self, page):
        if self._cookies_done:
            return
        for label in ["Aceitar tudo", "Accept all", "Concordar com tudo", "Tout accepter"]:
            try:
                page.get_by_role("button", name=label).click(timeout=2500)
                self._cookies_done = True
                time.sleep(1)
                return
            except Exception:
                pass

    def _fill_airport(self, page, nth, code):
        """Preenche o N-ésimo campo de aeroporto (0=origem, 1=destino)."""
        inputs = page.get_by_role("combobox").all()
        # Tenta diferentes índices: os primeiros comboboxes são tipo de viagem
        # e aeroportos. Varia conforme o layout atual do Google Flights.
        candidates = inputs[nth:nth+3]
        for inp in candidates:
            try:
                inp.click(timeout=3000)
                time.sleep(0.4)
                inp.press("Control+a")
                inp.type(code, delay=90)
                time.sleep(1.8)
                opts = page.get_by_role("option").all()
                if opts:
                    opts[0].click()
                    time.sleep(0.6)
                    return True
            except Exception:
                continue
        return False

    def _set_date(self, page, date_str, field_aria_keywords):
        """
        Tenta preencher um campo de data.
        Estratégia: clicar no campo, limpar, digitar a data, confirmar.
        """
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        # Formatos a tentar na ordem
        formats = [
            date_str,                                          # 2026-09-15
            f"{dt.day}/{dt.month}/{dt.year}",                  # 15/9/2026
            f"{dt.day:02d}/{dt.month:02d}/{dt.year}",          # 15/09/2026
            f"{dt.day} de {MONTHS_LONG[dt.month]} de {dt.year}", # 15 de setembro de 2026
        ]
        # Seletores a tentar
        selectors = []
        for kw in field_aria_keywords:
            selectors.append(f'input[aria-label*="{kw}"]')
            selectors.append(f'[aria-label*="{kw}"]')

        field = None
        for sel in selectors:
            try:
                el = page.locator(sel).first
                el.wait_for(timeout=3000)
                field = el
                break
            except Exception:
                continue

        if not field:
            return False

        for fmt in formats:
            try:
                field.click(timeout=3000)
                time.sleep(0.5)
                field.press("Control+a")
                field.type(fmt, delay=60)
                time.sleep(0.5)
                field.press("Enter")
                time.sleep(0.8)
                return True
            except Exception:
                pass
        return False

    # ── Busca principal ────────────────────────────────────────────────────

    def search(self, origin, dest, dep_date, ret_date):
        """
        Pesquisa origin→dest no Google Flights.
        Retorna (price: float | None, airline: str).
        """
        page = self.ctx.new_page()
        page.set_default_timeout(18000)
        try:
            return self._search(page, origin, dest, dep_date, ret_date)
        except Exception as e:
            print(f"    [erro geral] {e}")
            return None, "—"
        finally:
            page.close()

    def _search(self, page, origin, dest, dep_date, ret_date):
        # 1. Abrir Google Flights
        page.goto(
            "https://www.google.com/travel/flights?hl=pt-BR&gl=BR&curr=BRL",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        time.sleep(random.uniform(2, 3))
        self._accept_cookies(page)
        time.sleep(0.8)

        # 2. Garantir "Ida e volta" selecionado
        try:
            sel = page.get_by_role("combobox").first
            txt = sel.inner_text(timeout=2000)
            if "somente" in txt.lower() or "one" in txt.lower():
                sel.click()
                time.sleep(0.4)
                page.get_by_role("option").filter(
                    has_text=re.compile(r"ida e volta|round", re.I)
                ).first.click()
                time.sleep(0.5)
        except Exception:
            pass

        # 3. Origem
        ok = self._fill_airport(page, 1, origin)
        if not ok:
            ok = self._fill_airport(page, 0, origin)
        if not ok:
            print(f"    [origem falhou]")
            return None, "—"

        # 4. Destino
        ok = self._fill_airport(page, 2, dest)
        if not ok:
            ok = self._fill_airport(page, 1, dest)
        if not ok:
            print(f"    [destino falhou]")
            return None, "—"

        # 5. Data de ida
        self._set_date(page, dep_date,
                       ["Partida", "Departure", "Data de partida", "Check-in"])

        # 6. Data de volta
        self._set_date(page, ret_date,
                       ["Volta", "Return", "Data de volta", "Check-out"])

        # 7. Clicar em pesquisar
        for btn_name in ["Pesquisar", "Search", "Buscar"]:
            try:
                page.get_by_role("button", name=btn_name).click(timeout=3000)
                break
            except Exception:
                pass
        else:
            page.keyboard.press("Enter")

        # 8. Aguardar resultados
        time.sleep(random.uniform(6, 10))
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(2)

        # 9. Extrair preços do texto da página
        body_text = page.inner_text("body")

        # Verificar se há CAPTCHA
        if "captcha" in body_text.lower() or "não sou um robô" in body_text.lower():
            print("    [CAPTCHA detectado — pulando rota]")
            return None, "—"

        prices = parse_brl_prices(body_text)
        if not prices:
            print("    [nenhum preço encontrado]")
            return None, "—"

        price = min(prices)
        airline = detect_airline(body_text)
        return price, airline

    def close(self):
        self.browser.close()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"▶ Flight Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC")

    today      = datetime.now().date()
    dep_date   = (today + timedelta(days=SEARCH_OFFSET)).strftime("%Y-%m-%d")
    ret_date   = (today + timedelta(days=SEARCH_OFFSET + TRIP_DURATION)).strftime("%Y-%m-%d")
    today_str  = today.strftime("%Y-%m-%d")
    cutoff     = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    print(f"  Buscando voos: ida {dep_date} · volta {ret_date}")

    # FTP: baixar histórico
    print("▶ FTP: baixando histórico...")
    ftp = ftplib.FTP(FTP_HOST, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    try:
        ftp.cwd(FTP_PATH)
    except ftplib.error_perm:
        parts = FTP_PATH.strip("/").split("/")
        cur = ""
        for p in parts:
            cur += f"/{p}"
            try:
                ftp.cwd(cur)
            except ftplib.error_perm:
                ftp.mkd(cur)
                ftp.cwd(cur)

    history = ftp_download_json(ftp, "history.json", default={})

    results = []
    alerts  = []

    with sync_playwright() as pw:
        scraper = GFlightsScraper(pw)

        for route in ROUTES:
            label = f"{route['from_city']} → {route['to_city']}"
            print(f"  → {route['id']}  {label}")

            price, airline = scraper.search(
                route["origin"], route["dest"], dep_date, ret_date
            )

            if price is None:
                print(f"     sem resultado")
                time.sleep(random.uniform(*DELAY_BETWEEN))
                continue

            # Atualizar histórico
            rid  = route["id"]
            hist = history.get(rid, [])
            hist = [h for h in hist if h["date"] != today_str and h["date"] >= cutoff]
            hist.append({"date": today_str, "price": price})
            history[rid] = hist

            prices_list = [h["price"] for h in hist]
            n           = len(prices_list)
            median      = statistics.median(prices_list) if n > 1 else price
            pct         = round((price - median) / median * 100, 1) if n > 1 else 0.0
            status      = get_status(pct, n)
            link        = build_kayak_link(route["origin"], route["dest"], dep_date, ret_date)

            result = {
                "id":         rid,
                "from":       route["from_city"],
                "to":         route["to_city"],
                "flag":       route["flag"],
                "origin":     route["origin"],
                "dest":       route["dest"],
                "airline":    airline,
                "price":      round(price),
                "dep_date":   dep_date,
                "ret_date":   ret_date,
                "dep_fmt":    fmt_date(dep_date),
                "ret_fmt":    fmt_date(ret_date),
                "median_30d": round(median),
                "pct_change": pct,
                "status":     status,
                "n_points":   n,
                "link":       link,
            }
            results.append(result)
            print(f"     R$ {price:,.0f} via {airline}  {pct:+.0f}%  [{status}]")

            # Alerta para tarifas excepcionais
            if status in ("error", "fire") and n >= MIN_POINTS_FOR_PCT:
                icon = "🚨 POSSÍVEL ERRO DE TARIFA" if status == "error" else "🔥 PROMOÇÃO EXCEPCIONAL"
                alerts.append(
                    f"{icon}\n\n"
                    f"✈️ <b>{route['from_city']} → {route['to_city']}</b>\n\n"
                    f"💰 Preço: <b>R$ {price:,.0f}</b>\n"
                    f"📊 Mediana 30d: R$ {median:,.0f}\n"
                    f"📉 Variação: <b>{pct:+.0f}%</b>\n"
                    f"🏢 Cia: {airline}\n"
                    f"📅 Ida: {fmt_date(dep_date)}  |  Volta: {fmt_date(ret_date)}\n\n"
                    f"🔗 <a href='{link}'>Ver no Kayak</a>"
                )

            time.sleep(random.uniform(*DELAY_BETWEEN))

        scraper.close()

    # Ordenar por variação (mais baratos primeiro)
    results.sort(key=lambda r: r["pct_change"] if r["status"] != "new" else 999)

    payload = {
        "updated_at":  datetime.utcnow().isoformat() + "Z",
        "updated_fmt": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "total":       len(results),
        "routes":      results,
    }

    # FTP: subir dados
    print("▶ FTP: subindo data.json e history.json...")
    ftp_upload_json(ftp, payload,  "data.json")
    ftp_upload_json(ftp, history,  "history.json")
    ftp.quit()

    print(f"✓ {len(results)} rotas | {len(alerts)} alertas")

    for msg in alerts:
        send_telegram(msg)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
