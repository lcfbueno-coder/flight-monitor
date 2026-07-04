"""
Flight Price Monitor — v2 (Playwright / Google Flights)

Mudanças principais em relação à v1:
  • Navegação DIRETA para a página de resultados via URL com ?q=
    ("Flights from GRU to LIS on ... through ...") — elimina toda a
    digitação/cliques no formulário, que era o ponto de falha da v1.
  • Cookie SOCS=CAI injetado no contexto — pula a tela de consentimento
    do Google que aparece em IPs de datacenter (GitHub Actions).
  • Extração de preço POR RESULTADO (cada <li> da lista): o preço e a
    companhia vêm da MESMA linha, e preços de outras seções da página
    (gráfico de preços, datas alternativas, anúncios) não contaminam o min().
  • Clique em "Mais voos" para revelar tarifas escondidas na lista.
  • Mediana de referência calculada só com DIAS ANTERIORES — o preço de
    hoje não entra na própria base e não dilui a queda que queremos detectar.
  • Histórico guarda o MENOR preço de cada dia (compatível com 2 rodadas/dia).
  • Retry por rota + screenshot de diagnóstico por rota que falhar.
  • Aviso no Telegram se metade ou mais das rotas falhar (possível bloqueio).
  • Horários exibidos em America/Sao_Paulo; datetime.utcnow() removido.
"""

import re
import json
import os
import statistics
import time
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ─── Config ──────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
OUTPUT_DIR       = os.environ.get("OUTPUT_DIR", "docs")

TRIP_DURATION      = 10          # dias de viagem
SEARCH_OFFSET      = 45          # busca sempre 45 dias à frente
MIN_POINTS_FOR_PCT = 5           # dias ANTERIORES mínimos p/ calcular variação
RETRIES_PER_ROUTE  = 2
DELAY_BETWEEN      = (6, 10)     # pausa entre rotas (s)
MIN_PRICE          = 400         # piso p/ preço extraído de um resultado
FALLBACK_MIN_PRICE = 900         # piso p/ fallback (página inteira = mais ruído)
MAX_PRICE          = 120_000
HISTORY_DAYS       = 30

TZ_BR = ZoneInfo("America/Sao_Paulo")

MONTHS_PT = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
             "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

KNOWN_AIRLINES = [
    "LATAM", "GOL", "Azul", "TAP", "Iberia", "Air France", "KLM",
    "Lufthansa", "Swiss", "Turkish Airlines", "EgyptAir", "Emirates",
    "Qatar Airways", "American Airlines", "United", "Delta",
    "British Airways", "Copa Airlines", "Aerolíneas Argentinas",
    "Sky Airline", "JetSMART", "ITA Airways", "Avianca", "Air Europa",
    "Aeroméxico", "Air Canada", "Flybondi", "Ethiopian",
]

ROUTES = [
    {"id": "GRU-CUN", "origin": "GRU", "dest": "CUN", "from_city": "São Paulo",     "to_city": "Cancún",       "flag": "🇲🇽"},
    {"id": "GRU-MIA", "origin": "GRU", "dest": "MIA", "from_city": "São Paulo",     "to_city": "Miami",        "flag": "🇺🇸"},
    {"id": "GRU-LIS", "origin": "GRU", "dest": "LIS", "from_city": "São Paulo",     "to_city": "Lisboa",       "flag": "🇵🇹"},
    {"id": "GRU-MAD", "origin": "GRU", "dest": "MAD", "from_city": "São Paulo",     "to_city": "Madri",        "flag": "🇪🇸"},
    {"id": "GRU-CDG", "origin": "GRU", "dest": "CDG", "from_city": "São Paulo",     "to_city": "Paris",        "flag": "🇫🇷"},
    {"id": "GRU-FCO", "origin": "GRU", "dest": "FCO", "from_city": "São Paulo",     "to_city": "Roma",         "flag": "🇮🇹"},
    {"id": "GRU-GVA", "origin": "GRU", "dest": "GVA", "from_city": "São Paulo",     "to_city": "Genebra",      "flag": "🇨🇭"},
    {"id": "GRU-IST", "origin": "GRU", "dest": "IST", "from_city": "São Paulo",     "to_city": "Istambul",     "flag": "🇹🇷"},
    {"id": "GRU-CAI", "origin": "GRU", "dest": "CAI", "from_city": "São Paulo",     "to_city": "Cairo",        "flag": "🇪🇬"},
    {"id": "GRU-DXB", "origin": "GRU", "dest": "DXB", "from_city": "São Paulo",     "to_city": "Dubai",        "flag": "🇦🇪"},
    {"id": "CWB-SCL", "origin": "CWB", "dest": "SCL", "from_city": "Curitiba",      "to_city": "Santiago",     "flag": "🇨🇱"},
    {"id": "CWB-EZE", "origin": "CWB", "dest": "EZE", "from_city": "Curitiba",      "to_city": "Buenos Aires", "flag": "🇦🇷"},
    {"id": "CWB-BRC", "origin": "CWB", "dest": "BRC", "from_city": "Curitiba",      "to_city": "Bariloche",    "flag": "🇦🇷"},
    {"id": "FLN-EZE", "origin": "FLN", "dest": "EZE", "from_city": "Florianópolis", "to_city": "Buenos Aires", "flag": "🇦🇷"},
    {"id": "FLN-BRC", "origin": "FLN", "dest": "BRC", "from_city": "Florianópolis", "to_city": "Bariloche",    "flag": "🇦🇷"},
]

# ─── Helpers ─────────────────────────────────────────────────────────────────

PRICE_RE = re.compile(r'R\$\s*([\d]{1,3}(?:\.[\d]{3})*)')


def parse_brl_prices(text, min_p):
    prices = []
    for m in PRICE_RE.findall(text):
        try:
            v = float(m.replace('.', ''))
            if min_p <= v <= MAX_PRICE:
                prices.append(v)
        except ValueError:
            pass
    return prices


def detect_airline(text):
    tl = text.lower()
    for a in KNOWN_AIRLINES:
        if a.lower() in tl:
            return a
    return "—"


def fmt_date(d):
    if not d:
        return "—"
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{dt.day:02d} {MONTHS_PT[dt.month - 1]}"


def build_kayak_link(o, d, dep, ret):
    return f"https://www.kayak.com.br/flights/{o}-{d}/{dep}/{ret}"


def build_gflights_url(origin, dest, dep, ret):
    """URL direta para a página de resultados — sem interação com formulário."""
    q = f"Flights from {origin} to {dest} on {dep} through {ret}"
    return ("https://www.google.com/travel/flights?q=" + quote(q)
            + "&hl=pt-BR&gl=BR&curr=BRL")


def get_status(pct, n_prev):
    """n_prev = quantidade de dias ANTERIORES no histórico."""
    if n_prev < MIN_POINTS_FOR_PCT:
        return "new"
    if pct <= -50:
        return "error"
    if pct <= -30:
        return "fire"
    if pct <= -10:
        return "good"
    if pct >= 20:
        return "high"
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
            timeout=10,
        )
    except Exception as e:
        print(f"  [Telegram] {e}")


# ─── Scraper ─────────────────────────────────────────────────────────────────

class GFlightsScraper:

    def __init__(self, pw):
        self.browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled", "--lang=pt-BR"],
        )
        self.ctx = self.browser.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1366, "height": 768},
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        )
        # SOCS=CAI → recusa cookies opcionais e PULA a tela de consentimento
        # que o Google mostra para IPs de datacenter (caso do GitHub Actions).
        self.ctx.add_cookies([{
            "name": "SOCS", "value": "CAI",
            "domain": ".google.com", "path": "/",
        }])
        self.ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

    # ── Consentimento (fallback; o cookie SOCS normalmente já resolve) ──
    def _dismiss_consent(self, page):
        for label in ["Aceitar tudo", "Accept all", "Recusar tudo", "Reject all"]:
            try:
                page.get_by_role("button", name=label).first.click(timeout=1500)
                time.sleep(0.8)
                return
            except Exception:
                pass

    def search(self, origin, dest, dep_date, ret_date):
        """Tenta a rota com retry. Retorna (preço, cia) ou (None, '—')."""
        for attempt in range(1, RETRIES_PER_ROUTE + 1):
            page = self.ctx.new_page()
            page.set_default_timeout(20000)
            try:
                price, airline = self._search(page, origin, dest, dep_date, ret_date)
                if price is not None:
                    return price, airline
                if attempt == RETRIES_PER_ROUTE:
                    self._debug_shot(page, origin, dest)
            except Exception as e:
                print(f"    [tentativa {attempt}] {type(e).__name__}: {e}")
                if attempt == RETRIES_PER_ROUTE:
                    self._debug_shot(page, origin, dest)
            finally:
                page.close()
            if attempt < RETRIES_PER_ROUTE:
                time.sleep(random.uniform(4, 7))
        return None, "—"

    @staticmethod
    def _debug_shot(page, origin, dest):
        try:
            page.screenshot(path=f"debug_{origin}-{dest}.png", full_page=False)
            print(f"    [debug] screenshot salvo: debug_{origin}-{dest}.png")
        except Exception:
            pass

    def _search(self, page, origin, dest, dep_date, ret_date):
        # ── 1. Ir DIRETO para os resultados ─────────────────────────────────
        url = build_gflights_url(origin, dest, dep_date, ret_date)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

        if "consent.google" in page.url:
            self._dismiss_consent(page)

        # ── 2. Esperar algum preço em R$ aparecer ────────────────────────────
        try:
            page.wait_for_selector(r"text=/R\$\s?\d/", timeout=25000)
        except PWTimeout:
            body = page.inner_text("body").lower()
            if ("captcha" in body or "não sou um robô" in body
                    or "unusual traffic" in body):
                print("    [CAPTCHA/bloqueio detectado]")
            else:
                print(f"    [sem preços — URL: {page.url[:90]}]")
            return None, "—"

        time.sleep(2.5)  # deixa a lista terminar de renderizar

        # ── 3. Expandir "Mais voos" (revela tarifas escondidas) ─────────────
        try:
            page.get_by_role(
                "button",
                name=re.compile("Mais voos|More flights|Ver mais voos|Show more", re.I),
            ).first.click(timeout=2500)
            time.sleep(1.8)
        except Exception:
            pass

        # ── 4. Extração POR RESULTADO: preço e cia da MESMA linha ───────────
        best_price, best_airline = None, "—"
        items = []
        try:
            main = page.locator('[role="main"]').first
            items = main.get_by_role("listitem").all()
        except Exception:
            pass
        if not items:
            try:
                items = page.get_by_role("listitem").all()
            except Exception:
                items = []

        for item in items[:60]:
            try:
                txt = item.inner_text(timeout=1500)
            except Exception:
                continue
            prices = parse_brl_prices(txt, MIN_PRICE)
            if not prices:
                continue
            p = min(prices)
            if best_price is None or p < best_price:
                best_price = p
                best_airline = detect_airline(txt)

        # ── 5. Fallback: página inteira (mais ruído → piso maior) ───────────
        if best_price is None:
            body = page.inner_text("body")
            prices = parse_brl_prices(body, FALLBACK_MIN_PRICE)
            if prices:
                best_price = min(prices)
                best_airline = detect_airline(body)
                print("    [fallback: extração da página inteira]")

        if best_price is None:
            print("    [nenhum preço válido extraído]")
            return None, "—"

        return best_price, best_airline

    def close(self):
        self.browser.close()


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    now_br = datetime.now(TZ_BR)
    print(f"▶ Flight Monitor — {now_br.strftime('%d/%m/%Y %H:%M')} (horário de Brasília)")

    today     = now_br.date()
    dep_date  = (today + timedelta(days=SEARCH_OFFSET)).strftime("%Y-%m-%d")
    ret_date  = (today + timedelta(days=SEARCH_OFFSET + TRIP_DURATION)).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")
    cutoff    = (today - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    print(f"  Ida: {dep_date}  |  Volta: {ret_date}")

    history_path = os.path.join(OUTPUT_DIR, "history.json")
    data_path    = os.path.join(OUTPUT_DIR, "data.json")
    history = load_json(history_path, default={})
    results, alerts, failed = [], [], []

    # Ordem aleatória a cada rodada — evita padrão fixo de requisições
    route_order = random.sample(ROUTES, len(ROUTES))

    with sync_playwright() as pw:
        scraper = GFlightsScraper(pw)
        for route in route_order:
            print(f"  → {route['id']}  {route['from_city']} → {route['to_city']}")
            price, airline = scraper.search(route["origin"], route["dest"],
                                            dep_date, ret_date)

            if price is None:
                failed.append(route["id"])
                print("     sem resultado")
                time.sleep(random.uniform(*DELAY_BETWEEN))
                continue

            rid  = route["id"]
            hist = [h for h in history.get(rid, []) if h["date"] >= cutoff]

            # Histórico: guarda o MENOR preço do dia (2 rodadas/dia)
            entry = next((h for h in hist if h["date"] == today_str), None)
            if entry:
                entry["price"] = min(entry["price"], price)
            else:
                hist.append({"date": today_str, "price": price})
            hist.sort(key=lambda h: h["date"])
            history[rid] = hist

            # Baseline: SÓ dias anteriores — hoje não dilui a própria queda
            prev   = [h["price"] for h in hist if h["date"] != today_str]
            n_prev = len(prev)
            med    = statistics.median(prev) if prev else price
            pct    = round((price - med) / med * 100, 1) if prev else 0.0
            stat   = get_status(pct, n_prev)
            link   = build_kayak_link(route["origin"], route["dest"], dep_date, ret_date)

            results.append({
                "id": rid, "from": route["from_city"], "to": route["to_city"],
                "flag": route["flag"], "origin": route["origin"], "dest": route["dest"],
                "airline": airline, "price": round(price),
                "dep_date": dep_date, "ret_date": ret_date,
                "dep_fmt": fmt_date(dep_date), "ret_fmt": fmt_date(ret_date),
                "median_30d": round(med), "pct_change": pct,
                "status": stat, "n_points": n_prev + 1, "link": link,
            })
            print(f"     R$ {price:,.0f} via {airline}  {pct:+.0f}%  [{stat}]")

            # get_status já exige MIN_POINTS_FOR_PCT dias anteriores
            if stat in ("error", "fire"):
                icon = ("🚨 POSSÍVEL ERRO DE TARIFA" if stat == "error"
                        else "🔥 PROMOÇÃO EXCEPCIONAL")
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
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_fmt": datetime.now(TZ_BR).strftime("%d/%m/%Y %H:%M"),
        "total": len(results),
        "failed": failed,
        "routes": results,
    }
    save_json(data_path, payload)
    save_json(history_path, history)
    print(f"✓ {len(results)}/{len(ROUTES)} rotas salvas em {OUTPUT_DIR}/")

    # Falha em massa = provável bloqueio → avisar em vez de falhar em silêncio
    if failed and len(failed) >= len(ROUTES) // 2:
        send_telegram(
            "⚠️ <b>Flight Monitor</b>: rodada com falha em massa — "
            f"{len(failed)}/{len(ROUTES)} rotas sem resultado "
            f"({', '.join(failed)}). Possível bloqueio do Google Flights."
        )

    for msg in alerts:
        send_telegram(msg)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
