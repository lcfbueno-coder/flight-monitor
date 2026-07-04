"""
Flight Price Monitor — v3 (Playwright / Google Flights)

Mudança central em relação à v2:
  • Em vez de monitorar UMA data fixa (D+45 → D+55), o script agora abre o
    CALENDÁRIO DE PREÇOS do Google Flights (o mesmo painel que mostra o menor
    preço por dia) e varre os próximos 90 dias, para viagens com a duração
    definida em TRIP_DURATIONS. O painel passa a mostrar a MENOR tarifa do
    trimestre por rota, com as datas exatas em que ela ocorre.
  • Antes de disparar alerta 🔥/🚨, o preço do calendário é VERIFICADO na
    página de resultados real (calendário é estimativa e pode estar defasado).
  • Se o calendário falhar numa rota, o script degrada para o modo antigo
    (busca em data fixa na âncora D+10) — o painel nunca fica vazio.

Mantido da v2: URL direta ?q= (sem digitação), cookie SOCS=CAI (pula
consentimento), extração por resultado, mediana só com dias anteriores,
menor preço do dia no histórico, retry, screenshots de debug, aviso de
falha em massa no Telegram, horários em America/Sao_Paulo.
"""

import re
import json
import os
import statistics
import time
import random
from datetime import datetime, date, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ─── Config ──────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
OUTPUT_DIR       = os.environ.get("OUTPUT_DIR", "docs")

# Durações de viagem a testar (dias). Para testar também 21 dias:
# TRIP_DURATIONS = [15, 21]  → dobra o nº de páginas por rodada.
TRIP_DURATIONS     = [15]
SEARCH_WINDOW      = 90        # varre a menor tarifa dos próximos N dias
ANCHOR_OFFSET      = 10        # data âncora da URL (o calendário abre perto de hoje)
CAL_MONTH_CLICKS   = 2         # avanços de mês no calendário (2 → cobre ~3 meses)
MIN_POINTS_FOR_PCT = 5         # dias ANTERIORES mínimos p/ calcular variação
RETRIES_PER_ROUTE  = 2
DELAY_BETWEEN      = (5, 8)    # pausa entre rotas (s)
MIN_PRICE          = 400       # piso p/ preço válido
FALLBACK_MIN_PRICE = 900       # piso p/ extração da página inteira (mais ruído)
MAX_PRICE          = 120_000
HISTORY_DAYS       = 30

TZ_BR = ZoneInfo("America/Sao_Paulo")

MONTHS_PT = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
             "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

MONTHS_FULL = {
    # pt-BR
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
    # en (seguro caso o Google force inglês no IP do runner)
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

KNOWN_AIRLINES = [
    "LATAM", "GOL", "Azul", "TAP", "Iberia", "Air France", "KLM",
    "Lufthansa", "Swiss", "Turkish Airlines", "EgyptAir", "Emirates",
    "Qatar Airways", "American Airlines", "United", "Delta",
    "British Airways", "Copa Airlines", "Aerolíneas Argentinas",
    "Sky Airline", "JetSMART", "ITA Airways", "Avianca", "Air Europa",
    "Aeroméxico", "Air Canada", "Flybondi", "Ethiopian", "El Al",
]

ROUTES = [
    # ── São Paulo (GRU) ──────────────────────────────────────────────────────
    {"id": "GRU-CUN", "origin": "GRU", "dest": "CUN", "from_city": "São Paulo",     "to_city": "Cancún",       "flag": "🇲🇽"},
    {"id": "GRU-MIA", "origin": "GRU", "dest": "MIA", "from_city": "São Paulo",     "to_city": "Miami",        "flag": "🇺🇸"},
    {"id": "GRU-ATL", "origin": "GRU", "dest": "ATL", "from_city": "São Paulo",     "to_city": "Atlanta",      "flag": "🇺🇸"},
    {"id": "GRU-LIS", "origin": "GRU", "dest": "LIS", "from_city": "São Paulo",     "to_city": "Lisboa",       "flag": "🇵🇹"},
    {"id": "GRU-MAD", "origin": "GRU", "dest": "MAD", "from_city": "São Paulo",     "to_city": "Madri",        "flag": "🇪🇸"},
    {"id": "GRU-CDG", "origin": "GRU", "dest": "CDG", "from_city": "São Paulo",     "to_city": "Paris",        "flag": "🇫🇷"},
    {"id": "GRU-FCO", "origin": "GRU", "dest": "FCO", "from_city": "São Paulo",     "to_city": "Roma",         "flag": "🇮🇹"},
    {"id": "GRU-GVA", "origin": "GRU", "dest": "GVA", "from_city": "São Paulo",     "to_city": "Genebra",      "flag": "🇨🇭"},
    {"id": "GRU-ZRH", "origin": "GRU", "dest": "ZRH", "from_city": "São Paulo",     "to_city": "Zurique",      "flag": "🇨🇭"},
    {"id": "GRU-IST", "origin": "GRU", "dest": "IST", "from_city": "São Paulo",     "to_city": "Istambul",     "flag": "🇹🇷"},
    {"id": "GRU-CAI", "origin": "GRU", "dest": "CAI", "from_city": "São Paulo",     "to_city": "Cairo",        "flag": "🇪🇬"},
    {"id": "GRU-DXB", "origin": "GRU", "dest": "DXB", "from_city": "São Paulo",     "to_city": "Dubai",        "flag": "🇦🇪"},
    {"id": "GRU-TLV", "origin": "GRU", "dest": "TLV", "from_city": "São Paulo",     "to_city": "Tel Aviv",     "flag": "🇮🇱"},
    {"id": "GRU-LIM", "origin": "GRU", "dest": "LIM", "from_city": "São Paulo",     "to_city": "Lima",         "flag": "🇵🇪"},
    {"id": "GRU-YVR", "origin": "GRU", "dest": "YVR", "from_city": "São Paulo",     "to_city": "Vancouver",    "flag": "🇨🇦"},
    # ── Curitiba (CWB) ───────────────────────────────────────────────────────
    {"id": "CWB-SCL", "origin": "CWB", "dest": "SCL", "from_city": "Curitiba",      "to_city": "Santiago",     "flag": "🇨🇱"},
    {"id": "CWB-EZE", "origin": "CWB", "dest": "EZE", "from_city": "Curitiba",      "to_city": "Buenos Aires", "flag": "🇦🇷"},
    {"id": "CWB-BRC", "origin": "CWB", "dest": "BRC", "from_city": "Curitiba",      "to_city": "Bariloche",    "flag": "🇦🇷"},
    {"id": "CWB-CUR", "origin": "CWB", "dest": "CUR", "from_city": "Curitiba",      "to_city": "Curaçao",      "flag": "🇨🇼"},
    {"id": "CWB-PUJ", "origin": "CWB", "dest": "PUJ", "from_city": "Curitiba",      "to_city": "Punta Cana",   "flag": "🇩🇴"},
    {"id": "CWB-MCZ", "origin": "CWB", "dest": "MCZ", "from_city": "Curitiba",      "to_city": "Maceió",       "flag": "🇧🇷"},
    {"id": "CWB-JPA", "origin": "CWB", "dest": "JPA", "from_city": "Curitiba",      "to_city": "João Pessoa",  "flag": "🇧🇷"},
    {"id": "CWB-REC", "origin": "CWB", "dest": "REC", "from_city": "Curitiba",      "to_city": "Recife",       "flag": "🇧🇷"},
    {"id": "CWB-VIX", "origin": "CWB", "dest": "VIX", "from_city": "Curitiba",      "to_city": "Vitória",      "flag": "🇧🇷"},
    # ── Florianópolis (FLN) ──────────────────────────────────────────────────
    {"id": "FLN-EZE", "origin": "FLN", "dest": "EZE", "from_city": "Florianópolis", "to_city": "Buenos Aires", "flag": "🇦🇷"},
    {"id": "FLN-BRC", "origin": "FLN", "dest": "BRC", "from_city": "Florianópolis", "to_city": "Bariloche",    "flag": "🇦🇷"},
    # ── Navegantes (NVT) ─────────────────────────────────────────────────────
    {"id": "NVT-EZE", "origin": "NVT", "dest": "EZE", "from_city": "Navegantes",    "to_city": "Buenos Aires", "flag": "🇦🇷"},
    {"id": "NVT-MCZ", "origin": "NVT", "dest": "MCZ", "from_city": "Navegantes",    "to_city": "Maceió",       "flag": "🇧🇷"},
    {"id": "NVT-JPA", "origin": "NVT", "dest": "JPA", "from_city": "Navegantes",    "to_city": "João Pessoa",  "flag": "🇧🇷"},
    {"id": "NVT-REC", "origin": "NVT", "dest": "REC", "from_city": "Navegantes",    "to_city": "Recife",       "flag": "🇧🇷"},
    {"id": "NVT-VIX", "origin": "NVT", "dest": "VIX", "from_city": "Navegantes",    "to_city": "Vitória",      "flag": "🇧🇷"},
]

# ─── Regex ───────────────────────────────────────────────────────────────────

PRICE_RE = re.compile(r'R\$\s*([\d]{1,3}(?:\.[\d]{3})*)')

# aria-labels do calendário: "R$ 4.770, sábado, 3 de agosto de 2026"
# ou "3 de agosto de 2026, 4.770 Reais brasileiros" (com/sem ano)
CAL_DATE_RE = re.compile(
    r'(\d{1,2})\s+(?:de\s+)?(' + '|'.join(MONTHS_FULL.keys()) + r')(?:\s+(?:de\s+)?(\d{4}))?',
    re.I,
)
CAL_PRICE_RE = re.compile(
    r'R\$\s*([\d]{1,3}(?:\.[\d]{3})*)|([\d]{1,3}(?:\.[\d]{3})*)\s*reais',
    re.I,
)

# ─── Helpers ─────────────────────────────────────────────────────────────────

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


def parse_calendar_labels(labels, today, window_end):
    """Extrai {date: menor_preço} dos aria-labels do calendário de preços."""
    found = {}
    for label in labels:
        if not label:
            continue
        dm = CAL_DATE_RE.search(label)
        pm = CAL_PRICE_RE.search(label)
        if not (dm and pm):
            continue
        try:
            day   = int(dm.group(1))
            month = MONTHS_FULL[dm.group(2).lower()]
            year  = int(dm.group(3)) if dm.group(3) else today.year
            d = date(year, month, day)
            if not dm.group(3) and d < today:
                d = date(year + 1, month, day)
        except (ValueError, KeyError):
            continue
        if not (today < d <= window_end):
            continue
        raw = pm.group(1) or pm.group(2)
        try:
            price = float(raw.replace('.', ''))
        except ValueError:
            continue
        if not (MIN_PRICE <= price <= MAX_PRICE):
            continue
        if d not in found or price < found[d]:
            found[d] = price
    return found


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
        # SOCS=CAI → pula a tela de consentimento em IPs de datacenter
        self.ctx.add_cookies([{
            "name": "SOCS", "value": "CAI",
            "domain": ".google.com", "path": "/",
        }])
        self.ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._cal_shot_done = False

    # ── Consentimento (fallback; o cookie SOCS normalmente já resolve) ──
    def _dismiss_consent(self, page):
        for label in ["Aceitar tudo", "Accept all", "Recusar tudo", "Reject all"]:
            try:
                page.get_by_role("button", name=label).first.click(timeout=1500)
                time.sleep(0.8)
                return
            except Exception:
                pass

    @staticmethod
    def _debug_shot(page, name):
        try:
            page.screenshot(path=f"debug_{name}.png", full_page=False)
            print(f"    [debug] screenshot salvo: debug_{name}.png")
        except Exception:
            pass

    # ── Rota completa: calendário de 90 dias, com retry ─────────────────────
    def search_route(self, origin, dest, today):
        """Retorna dict(price, dep, ret, duration, airline, source) ou None."""
        window_end = today + timedelta(days=SEARCH_WINDOW)
        for attempt in range(1, RETRIES_PER_ROUTE + 1):
            best = None
            for dur in TRIP_DURATIONS:
                page = self.ctx.new_page()
                page.set_default_timeout(20000)
                try:
                    cand = self._scan_duration(page, origin, dest, today,
                                               window_end, dur)
                    if cand and (best is None or cand["price"] < best["price"]):
                        best = cand
                except Exception as e:
                    print(f"    [{dur}d, tentativa {attempt}] "
                          f"{type(e).__name__}: {e}")
                    if attempt == RETRIES_PER_ROUTE:
                        self._debug_shot(page, f"{origin}-{dest}")
                finally:
                    page.close()
                if len(TRIP_DURATIONS) > 1:
                    time.sleep(random.uniform(3, 5))
            if best:
                return best
            if attempt < RETRIES_PER_ROUTE:
                time.sleep(random.uniform(5, 8))
        return None

    def _scan_duration(self, page, origin, dest, today, window_end, dur):
        # 1. Abrir resultados numa data âncora próxima (o calendário abre
        #    mostrando os meses a partir de agora)
        anchor_dep = today + timedelta(days=ANCHOR_OFFSET)
        anchor_ret = anchor_dep + timedelta(days=dur)
        dep_a, ret_a = anchor_dep.isoformat(), anchor_ret.isoformat()

        page.goto(build_gflights_url(origin, dest, dep_a, ret_a),
                  wait_until="domcontentloaded", timeout=45000)
        if "consent.google" in page.url:
            self._dismiss_consent(page)

        try:
            page.wait_for_selector(r"text=/R\$\s?\d/", timeout=25000)
        except PWTimeout:
            body = page.inner_text("body").lower()
            if ("captcha" in body or "não sou um robô" in body
                    or "unusual traffic" in body):
                print("    [CAPTCHA/bloqueio detectado]")
            else:
                print(f"    [sem preços — URL: {page.url[:90]}]")
            return None
        time.sleep(2.0)

        # 2. Calendário de preços: menor tarifa por dia, próximos 90 dias
        cal = self._scan_calendar(page, today, window_end)
        if cal:
            dep_best, price = min(cal.items(), key=lambda kv: kv[1])
            ret_best = dep_best + timedelta(days=dur)
            print(f"    calendário: {len(cal)} dias lidos, "
                  f"mínimo R$ {price:,.0f} em {dep_best.isoformat()}")
            return {"price": price, "dep": dep_best.isoformat(),
                    "ret": ret_best.isoformat(), "duration": dur,
                    "airline": "—", "source": "calendar"}

        # 3. Fallback: modo antigo — melhor preço na data âncora
        print("    [calendário indisponível → fallback em data fixa]")
        price, airline = self._extract_results(page)
        if price is not None:
            return {"price": price, "dep": dep_a, "ret": ret_a,
                    "duration": dur, "airline": airline, "source": "fixed"}
        return None

    # ── Calendário ───────────────────────────────────────────────────────────
    def _open_price_panel(self, page):
        openers = [
            lambda: page.get_by_role("textbox",
                     name=re.compile("Partida|Departure", re.I)).first,
            lambda: page.locator('input[aria-label*="Partida"]').first,
            lambda: page.locator('[aria-label*="Partida"]').first,
            lambda: page.get_by_role("button",
                     name=re.compile("Gráfico de preços|Price graph", re.I)).first,
        ]
        for get in openers:
            try:
                get().click(timeout=2500)
            except Exception:
                continue
            try:
                dlg = page.locator('[role="dialog"]').first
                dlg.wait_for(state="visible", timeout=5000)
                time.sleep(1.5)
                return dlg
            except Exception:
                continue
        return None

    @staticmethod
    def _labels(dlg):
        try:
            return dlg.evaluate(
                "el => Array.from(el.querySelectorAll('[aria-label]'))"
                ".map(e => e.getAttribute('aria-label'))"
            ) or []
        except Exception:
            return []

    def _scan_calendar(self, page, today, window_end):
        dlg = self._open_price_panel(page)
        if dlg is None:
            print("    [não consegui abrir o calendário]")
            return {}

        prices = {}
        for i in range(CAL_MONTH_CLICKS + 1):
            found = {}
            for _ in range(5):  # espera os preços hidratarem (até ~7s)
                found = parse_calendar_labels(self._labels(dlg),
                                              today, window_end)
                if found:
                    break
                time.sleep(1.5)
            for d, p in found.items():
                if d not in prices or p < prices[d]:
                    prices[d] = p
            if i < CAL_MONTH_CLICKS:
                try:
                    dlg.get_by_role(
                        "button", name=re.compile(r"Próximo|Next", re.I)
                    ).first.click(timeout=2500)
                    time.sleep(1.8)
                except Exception:
                    break

        # Screenshot de calibração do calendário (uma vez por rodada)
        if not self._cal_shot_done:
            self._debug_shot(page, "calendar")
            self._cal_shot_done = True

        try:
            page.keyboard.press("Escape")
            time.sleep(0.5)
        except Exception:
            pass
        return prices

    # ── Extração da lista de resultados (fallback e verificação) ────────────
    def _extract_results(self, page):
        # Expandir "Mais voos" (revela tarifas escondidas)
        try:
            page.get_by_role(
                "button",
                name=re.compile("Mais voos|More flights|Ver mais voos|Show more", re.I),
            ).first.click(timeout=2500)
            time.sleep(1.8)
        except Exception:
            pass

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
            found = parse_brl_prices(txt, MIN_PRICE)
            if not found:
                continue
            p = min(found)
            if best_price is None or p < best_price:
                best_price = p
                best_airline = detect_airline(txt)

        if best_price is None:
            body = page.inner_text("body")
            found = parse_brl_prices(body, FALLBACK_MIN_PRICE)
            if found:
                best_price = min(found)
                best_airline = detect_airline(body)
                print("    [fallback: extração da página inteira]")

        return best_price, best_airline

    def results_probe(self, origin, dest, dep, ret):
        """Confirma o preço real numa data específica (usado antes de alertar)."""
        page = self.ctx.new_page()
        page.set_default_timeout(20000)
        try:
            page.goto(build_gflights_url(origin, dest, dep, ret),
                      wait_until="domcontentloaded", timeout=45000)
            if "consent.google" in page.url:
                self._dismiss_consent(page)
            page.wait_for_selector(r"text=/R\$\s?\d/", timeout=25000)
            time.sleep(2.0)
            return self._extract_results(page)
        except Exception as e:
            print(f"    [verificação falhou] {type(e).__name__}: {e}")
            return None, "—"
        finally:
            page.close()

    def close(self):
        self.browser.close()


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    now_br = datetime.now(TZ_BR)
    print(f"▶ Flight Monitor v3 — {now_br.strftime('%d/%m/%Y %H:%M')} "
          f"(horário de Brasília)")
    print(f"  Janela: {SEARCH_WINDOW} dias  |  "
          f"Durações: {TRIP_DURATIONS} dias de viagem")

    today     = now_br.date()
    today_str = today.strftime("%Y-%m-%d")
    cutoff    = (today - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")

    history_path = os.path.join(OUTPUT_DIR, "history.json")
    data_path    = os.path.join(OUTPUT_DIR, "data.json")
    history = load_json(history_path, default={})
    results, alerts, failed = [], [], []

    route_order = random.sample(ROUTES, len(ROUTES))

    with sync_playwright() as pw:
        scraper = GFlightsScraper(pw)
        for route in route_order:
            print(f"  → {route['id']}  {route['from_city']} → {route['to_city']}")
            best = scraper.search_route(route["origin"], route["dest"], today)

            if best is None:
                failed.append(route["id"])
                print("     sem resultado")
                time.sleep(random.uniform(*DELAY_BETWEEN))
                continue

            price   = best["price"]
            dep     = best["dep"]
            ret     = best["ret"]
            dur     = best["duration"]
            airline = best["airline"]
            source  = best["source"]

            rid  = route["id"]
            hist = [h for h in history.get(rid, []) if h["date"] >= cutoff]
            prev   = [h["price"] for h in hist if h["date"] != today_str]
            n_prev = len(prev)
            med    = statistics.median(prev) if prev else price
            pct    = round((price - med) / med * 100, 1) if prev else 0.0
            stat   = get_status(pct, n_prev)

            # Verificação: calendário é estimativa → confirmar antes de alertar
            if stat in ("error", "fire") and source == "calendar":
                print("     verificando tarifa na página de resultados…")
                vprice, vair = scraper.results_probe(route["origin"],
                                                     route["dest"], dep, ret)
                if vprice is not None:
                    price = vprice
                    if vair != "—":
                        airline = vair
                    pct  = round((price - med) / med * 100, 1) if prev else 0.0
                    stat = get_status(pct, n_prev)
                    source = "verified"

            # Histórico: menor preço do dia (compatível com 2 rodadas/dia)
            entry = next((h for h in hist if h["date"] == today_str), None)
            if entry:
                entry["price"] = min(entry["price"], price)
            else:
                hist.append({"date": today_str, "price": price})
            hist.sort(key=lambda h: h["date"])
            history[rid] = hist

            link = build_kayak_link(route["origin"], route["dest"], dep, ret)
            results.append({
                "id": rid, "from": route["from_city"], "to": route["to_city"],
                "flag": route["flag"], "origin": route["origin"],
                "dest": route["dest"], "airline": airline,
                "price": round(price), "dep_date": dep, "ret_date": ret,
                "dep_fmt": fmt_date(dep), "ret_fmt": fmt_date(ret),
                "duration": dur, "source": source,
                "median_30d": round(med), "pct_change": pct,
                "status": stat, "n_points": n_prev + 1, "link": link,
            })
            print(f"     R$ {price:,.0f} via {airline}  {pct:+.0f}%  "
                  f"[{stat}] ({source}, ida {dep})")

            # get_status já exige MIN_POINTS_FOR_PCT dias anteriores
            if stat in ("error", "fire"):
                icon = ("🚨 POSSÍVEL ERRO DE TARIFA" if stat == "error"
                        else "🔥 PROMOÇÃO EXCEPCIONAL")
                alerts.append(
                    f"{icon}\n\n✈️ <b>{route['from_city']} → {route['to_city']}</b>\n\n"
                    f"💰 Preço: <b>R$ {price:,.0f}</b>\n"
                    f"📊 Mediana 30d: R$ {med:,.0f}\n"
                    f"📉 Variação: <b>{pct:+.0f}%</b>\n🏢 Cia: {airline}\n"
                    f"📅 Ida: {fmt_date(dep)}  |  Volta: {fmt_date(ret)} "
                    f"({dur} dias)\n\n"
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
        "window_days": SEARCH_WINDOW,
        "durations": TRIP_DURATIONS,
        "routes": results,
    }
    save_json(data_path, payload)
    save_json(history_path, history)
    print(f"✓ {len(results)}/{len(ROUTES)} rotas salvas em {OUTPUT_DIR}/")

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
