"""
Flight Price Monitor — v3.1 (Playwright / Google Flights)

Correções sobre a v3 (que caiu em fallback em 100% das rotas):
  • A detecção do calendário deixou de depender de estrutura ([role=dialog]
    visível — o Google pré-renderiza dialogs OCULTOS e o teste pegava o
    primeiro deles). Agora o teste é por DADOS: clicou → apareceram preços
    por dia em qualquer lugar da página → calendário aberto.
  • Parser do calendário em 3 camadas: (1) aria-label com data+preço;
    (2) aria-label com data + preço no texto da célula; (3) célula
    "dia\npreço" com o mês vindo da própria grade.
  • Extração de resultados por <li> coletados via JavaScript
    (independe de roles de acessibilidade, que não estavam casando).
  • Âncora do fallback em data fixa: D+45 (não mais D+10 — tarifa de
    última hora inflava os preços). Janela do calendário: D+30 a D+90.
  • Diagnóstico que dispara NA FALHA: se o calendário não abrir, salva
    screenshot + HTML da página (debug_calendar_fail.*). A primeira rota
    também salva sempre debug_results.* para calibração.
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
MIN_DEP_OFFSET     = 30        # só considera partidas a partir de D+30...
SEARCH_WINDOW      = 90        # ...até D+90
ANCHOR_OFFSET      = 45        # âncora da URL (o fallback fixo também usa)
CAL_MONTH_CLICKS   = 1         # avanços de mês no calendário
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
    {"id": "GRU-MCO", "origin": "GRU", "dest": "MCO", "from_city": "São Paulo",     "to_city": "Orlando",      "flag": "🇺🇸"},
    {"id": "GRU-LIS", "origin": "GRU", "dest": "LIS", "from_city": "São Paulo",     "to_city": "Lisboa",       "flag": "🇵🇹"},
    {"id": "GRU-MAD", "origin": "GRU", "dest": "MAD", "from_city": "São Paulo",     "to_city": "Madri",        "flag": "🇪🇸"},
    {"id": "GRU-CDG", "origin": "GRU", "dest": "CDG", "from_city": "São Paulo",     "to_city": "Paris",        "flag": "🇫🇷"},
    {"id": "GRU-FCO", "origin": "GRU", "dest": "FCO", "from_city": "São Paulo",     "to_city": "Roma",         "flag": "🇮🇹"},
    {"id": "GRU-MXP", "origin": "GRU", "dest": "MXP", "from_city": "São Paulo",     "to_city": "Milão",        "flag": "🇮🇹"},
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

_MONTHS_ALT = '|'.join(MONTHS_FULL.keys())

# "3 de agosto de 2026" / "3 de agosto" / "3 August 2026"
CAL_DATE_RE = re.compile(
    r'(?<!\d)(\d{1,2})\s+(?:de\s+)?\b(' + _MONTHS_ALT + r')\b(?:\s+(?:de\s+)?(\d{4}))?',
    re.I,
)
# "agosto de 2026" (cabeçalho/aria-label da grade do mês)
MONTH_WORD_RE = re.compile(
    r'\b(' + _MONTHS_ALT + r')\b(?:\s+(?:de\s+)?(\d{4}))?', re.I,
)
# "R$ 4.770" ou "4.770 reais"
CAL_PRICE_RE = re.compile(
    r'R\$\s*([\d]{1,3}(?:\.[\d]{3})*)|([\d]{1,3}(?:\.[\d]{3})*)\s*reais',
    re.I,
)
# célula do calendário: "3\n4.770"
CELL_TEXT_RE = re.compile(
    r'^\s*(\d{1,2})\s*[\n\r]+\s*R?\$?\s*([\d]{1,3}(?:\.[\d]{3})*)\s*$',
)
ANY_NUMBER_RE = re.compile(r'([\d]{1,3}(?:\.[\d]{3})*)')
# ", 7398 Reais brasileiros" (preço exato no aria-label do preço)
REAIS_LABEL_RE = re.compile(r'([\d][\d.,]*)\s*reais', re.I)
# "10,9 mil" (formato abreviado exibido na célula)
MIL_TEXT_RE = re.compile(r'([\d]{1,3}(?:[.,]\d)?)\s*mil', re.I)


def _price_from_cell(label, text):
    """Preço da célula: exato do aria-label; senão do texto ('7.400'/'10,9 mil')."""
    if label:
        m = REAIS_LABEL_RE.search(label)
        if m:
            try:
                return float(m.group(1).replace('.', '').replace(',', '.'))
            except ValueError:
                pass
    if text:
        m = MIL_TEXT_RE.search(text)
        if m:
            try:
                return float(m.group(1).replace(',', '.')) * 1000
            except ValueError:
                pass
        cands = []
        for x in ANY_NUMBER_RE.findall(text):
            try:
                v = float(x.replace('.', ''))
            except ValueError:
                continue
            if MIN_PRICE <= v <= MAX_PRICE:
                cands.append(v)
        if cands:
            return min(cands)
    return None

# Coleta: células do calendário via data-iso (DOM real verificado em 04/07/2026:
# <div role="gridcell" data-iso="2026-07-05"><div aria-label="domingo, 5 de
# julho...">5</div><div aria-label=", 7398 Reais brasileiros">7.400</div></div>)
# + aria-labels da página como fallback.
COLLECT_JS = """
() => {
  const out = [];
  document.querySelectorAll('[data-iso]').forEach(c => {
    const p = c.querySelector('[aria-label*="eais"]');
    out.push({iso: c.getAttribute('data-iso'),
              p: p ? p.getAttribute('aria-label') : null,
              t: c.innerText});
  });
  document.querySelectorAll('[aria-label]').forEach(e => {
    out.push({l: e.getAttribute('aria-label'), t: null, m: null});
  });
  document.querySelectorAll('[role="grid"]').forEach(g => {
    const cap = g.querySelector('caption');
    const m = g.getAttribute('aria-label') || (cap ? cap.innerText : null);
    g.querySelectorAll('[role="gridcell"], [role="button"], td').forEach(c => {
      out.push({l: c.getAttribute('aria-label'), t: c.innerText, m: m});
    });
  });
  return out;
}
"""

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


def _mk_date(day, month, year):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_calendar_entries(entries, today, window_start, window_end):
    """Extrai {date: menor_preço} da coleta da página (3 camadas de parse)."""
    found = {}

    def keep(d, price):
        if d is None:
            return
        if not (window_start <= d <= window_end):
            return
        if not (MIN_PRICE <= price <= MAX_PRICE):
            return
        if d not in found or price < found[d]:
            found[d] = price

    for e in entries:
        # Camada 0: célula com data-iso (data ISO pronta + preço exato)
        iso = e.get("iso")
        if iso:
            try:
                d = date.fromisoformat(iso)
            except ValueError:
                continue
            price = _price_from_cell(e.get("p"), e.get("t"))
            if price is not None:
                keep(d, price)
            continue

        label = e.get("l") or ""
        text  = e.get("t") or ""
        mctx  = e.get("m") or ""

        dm = CAL_DATE_RE.search(label)
        if dm:
            day, month = int(dm.group(1)), MONTHS_FULL[dm.group(2).lower()]
            year = int(dm.group(3)) if dm.group(3) else today.year
            d = _mk_date(day, month, year)
            if d and not dm.group(3) and d < today:
                d = _mk_date(day, month, year + 1)

            # Camada 1: preço no próprio aria-label
            pm = CAL_PRICE_RE.search(label)
            if pm:
                raw = pm.group(1) or pm.group(2)
                try:
                    keep(d, float(raw.replace('.', '')))
                except ValueError:
                    pass
                continue

            # Camada 2: data no aria-label, preço no texto da célula
            if text:
                cands = []
                for x in ANY_NUMBER_RE.findall(text):
                    try:
                        v = float(x.replace('.', ''))
                    except ValueError:
                        continue
                    if MIN_PRICE <= v <= MAX_PRICE:
                        cands.append(v)
                if cands:
                    keep(d, min(cands))
            continue

        # Camada 3: célula "dia\npreço" com o mês vindo da grade
        if text and mctx:
            cm = CELL_TEXT_RE.match(text)
            mm = MONTH_WORD_RE.search(mctx)
            if cm and mm:
                day   = int(cm.group(1))
                month = MONTHS_FULL[mm.group(1).lower()]
                year  = int(mm.group(2)) if mm.group(2) else today.year
                d = _mk_date(day, month, year)
                if d and not mm.group(2) and d < today:
                    d = _mk_date(day, month, year + 1)
                try:
                    keep(d, float(cm.group(2).replace('.', '')))
                except ValueError:
                    pass

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
    if n_prev < MIN_POINTS_FOR_PCT:
        return "new"
    if pct <= -50:
        return "error"
    if pct <= -30:
        return "fire"
    if pct <= -10:
        return "good"    # verde a partir de -10%
    if pct >= 20:
        return "high"    # vermelho a partir de +20%
    return "normal"      # neutro entre -10% e +20%


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
        self.ctx.add_cookies([{
            "name": "SOCS", "value": "CAI",
            "domain": ".google.com", "path": "/",
        }])
        self.ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._results_dumped  = False
        self._noprices_dumped = False
        self._cal_fail_dumped = False
        self._cal_ok_dumped   = False

    # ── Diagnóstico ──────────────────────────────────────────────────────────
    @staticmethod
    def _dump(page, tag):
        try:
            page.screenshot(path=f"debug_{tag}.png", full_page=False)
        except Exception:
            pass
        try:
            with open(f"debug_{tag}.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            print(f"    [debug] debug_{tag}.png/.html salvos")
        except Exception:
            pass

    def _dismiss_consent(self, page):
        for label in ["Aceitar tudo", "Accept all", "Recusar tudo", "Reject all"]:
            try:
                page.get_by_role("button", name=label).first.click(timeout=1500)
                time.sleep(0.8)
                return
            except Exception:
                pass

    # ── Rota completa, com retry ─────────────────────────────────────────────
    def search_route(self, origin, dest, today):
        """Retorna dict(price, dep, ret, duration, airline, source) ou None."""
        window_start = today + timedelta(days=MIN_DEP_OFFSET)
        window_end   = today + timedelta(days=SEARCH_WINDOW)
        for attempt in range(1, RETRIES_PER_ROUTE + 1):
            best = None
            for dur in TRIP_DURATIONS:
                page = self.ctx.new_page()
                page.set_default_timeout(20000)
                try:
                    cand = self._scan_duration(page, origin, dest, today,
                                               window_start, window_end, dur)
                    if cand and (best is None or cand["price"] < best["price"]):
                        best = cand
                except Exception as e:
                    print(f"    [{dur}d, tentativa {attempt}] "
                          f"{type(e).__name__}: {e}")
                    if attempt == RETRIES_PER_ROUTE:
                        self._dump(page, f"{origin}-{dest}")
                finally:
                    page.close()
                if len(TRIP_DURATIONS) > 1:
                    time.sleep(random.uniform(3, 5))
            if best:
                return best
            if attempt < RETRIES_PER_ROUTE:
                time.sleep(random.uniform(5, 8))
        return None

    def _scan_duration(self, page, origin, dest, today,
                       window_start, window_end, dur):
        anchor_dep = today + timedelta(days=ANCHOR_OFFSET)
        anchor_ret = anchor_dep + timedelta(days=dur)
        dep_a, ret_a = anchor_dep.isoformat(), anchor_ret.isoformat()

        page.goto(build_gflights_url(origin, dest, dep_a, ret_a),
                  wait_until="domcontentloaded", timeout=45000)
        if "consent.google" in page.url:
            self._dismiss_consent(page)

        try:
            page.wait_for_selector(r"text=/R\$\s?\d/", timeout=35000)
        except PWTimeout:
            body = page.inner_text("body").lower()
            if ("captcha" in body or "não sou um robô" in body
                    or "unusual traffic" in body):
                print("    [CAPTCHA/bloqueio detectado]")
                return None
            if "algo deu errado" in body or "something went wrong" in body:
                # Erro transitório do Google — a própria página oferece Atualizar
                print("    ['Algo deu errado' → clicando Atualizar]")
                try:
                    page.get_by_role(
                        "button", name=re.compile("Atualizar|Refresh", re.I)
                    ).first.click(timeout=3000)
                except Exception:
                    page.reload(wait_until="domcontentloaded")
                try:
                    page.wait_for_selector(r"text=/R\$\s?\d/", timeout=25000)
                except PWTimeout:
                    print("    [sem preços mesmo após atualizar]")
                    return None
            else:
                print(f"    [sem preços — URL: {page.url[:90]}]")
                if not self._noprices_dumped:
                    self._dump(page, "noprices")
                    self._noprices_dumped = True
                return None
        time.sleep(2.0)

        # Dump de calibração da página de resultados (primeira rota)
        if not self._results_dumped:
            self._dump(page, "results")
            self._results_dumped = True

        # Calendário: menor tarifa por dia entre D+30 e D+90
        cal = self._scan_calendar(page, today, window_start, window_end)
        if cal:
            dep_best, price = min(cal.items(), key=lambda kv: kv[1])
            ret_best = dep_best + timedelta(days=dur)
            print(f"    calendário: {len(cal)} dias lidos, "
                  f"mínimo R$ {price:,.0f} em {dep_best.isoformat()}")
            return {"price": price, "dep": dep_best.isoformat(),
                    "ret": ret_best.isoformat(), "duration": dur,
                    "airline": "—", "source": "calendar"}

        # Fallback: melhor preço na data âncora (D+45)
        print("    [calendário indisponível → fallback em data fixa D+45]")
        price, airline = self._extract_results(page)
        if price is not None:
            return {"price": price, "dep": dep_a, "ret": ret_a,
                    "duration": dur, "airline": airline, "source": "fixed"}
        return None

    # ── Calendário ───────────────────────────────────────────────────────────
    def _collect(self, page):
        try:
            return page.evaluate(COLLECT_JS) or []
        except Exception:
            return []

    def _open_price_panel(self, page, today, window_start, window_end):
        """Clica em candidatos e valida por DADOS: apareceram preços por dia?

        Distingue dois estados (visto nos dumps de 04/07/2026):
          • calendário ABERTO mas sem a camada de preços → espera mais
            (até ~15 s) e dá uma cutucada (Avançar dispara novo fetch);
          • clique não abriu nada → tenta o próximo candidato.
        """
        openers = [
            'input[aria-label*="Partida" i]:visible',
            'input[aria-label*="Departure" i]:visible',
            '[aria-label*="Data de partida" i]:visible',
            'input[placeholder*="Partida" i]:visible',
            '[aria-label*="Gráfico de preços" i]:visible',
            '[aria-label*="Price graph" i]:visible',
        ]
        for sel in openers:
            try:
                page.locator(sel).first.click(timeout=2500)
            except Exception:
                continue
            calendar_open = False
            for i in range(10):  # até ~15 s
                time.sleep(1.5)
                entries = self._collect(page)
                if parse_calendar_entries(entries, today,
                                          window_start, window_end):
                    print(f"    calendário aberto ({sel.split(':')[0]})")
                    return True
                n_dates = sum(
                    1 for e in entries
                    if e.get("iso")
                    or (e.get("l") and CAL_DATE_RE.search(e["l"]))
                )
                if n_dates >= 15:
                    calendar_open = True
                    if i == 5:
                        self._advance_month(page)  # cutucada
                elif not calendar_open and i >= 2:
                    break  # este clique não abriu nada — próximo candidato
            if calendar_open:
                print("    [calendário abriu, mas o Google não retornou "
                      "a camada de preços]")
                return False  # inútil clicar os demais candidatos
        return False

    def _advance_month(self, page):
        # DOM real (04/07/2026): a seta tem aria-label="Avançar"
        for sel in ['[aria-label="Avançar"]:visible',
                    '[aria-label="Próximo"]:visible',
                    '[aria-label="Next"]:visible']:
            try:
                page.locator(sel).first.click(timeout=1500)
                time.sleep(1.8)
                return True
            except Exception:
                continue
        try:
            btns = page.get_by_role(
                "button", name=re.compile(r"Avançar|Próximo|Next", re.I)
            ).all()
        except Exception:
            return False
        for btn in btns[:6]:
            try:
                if btn.is_visible():
                    btn.click(timeout=1500)
                    time.sleep(1.8)
                    return True
            except Exception:
                continue
        return False

    def _scan_calendar(self, page, today, window_start, window_end):
        if not self._open_price_panel(page, today, window_start, window_end):
            if not self._cal_fail_dumped:
                self._dump(page, "calendar_fail")
                self._cal_fail_dumped = True
            print("    [não consegui abrir o calendário]")
            return {}

        prices = {}
        for i in range(CAL_MONTH_CLICKS + 1):
            found = {}
            for _ in range(4):
                found = parse_calendar_entries(self._collect(page), today,
                                               window_start, window_end)
                if found:
                    break
                time.sleep(1.4)
            for d, p in found.items():
                if d not in prices or p < prices[d]:
                    prices[d] = p
            if i < CAL_MONTH_CLICKS and not self._advance_month(page):
                break

        if prices and not self._cal_ok_dumped:
            self._dump(page, "calendar_ok")
            self._cal_ok_dumped = True

        try:
            page.keyboard.press("Escape")
            time.sleep(0.5)
        except Exception:
            pass
        return prices

    # ── Extração da lista de resultados (fallback e verificação) ────────────
    def _expand_more(self, page):
        for sel in ['[aria-label*="Mais voos" i]:visible',
                    '[aria-label*="More flights" i]:visible']:
            try:
                page.locator(sel).first.click(timeout=2000)
                time.sleep(1.8)
                return
            except Exception:
                pass
        try:
            page.get_by_role(
                "button",
                name=re.compile("Mais voos|More flights|Show more", re.I),
            ).first.click(timeout=2000)
            time.sleep(1.8)
        except Exception:
            pass

    def _extract_results(self, page):
        self._expand_more(page)

        best_price, best_airline = None, "—"
        try:
            texts = page.evaluate(
                "() => Array.from(document.querySelectorAll('li'))"
                ".map(e => e.innerText)"
            ) or []
        except Exception:
            texts = []

        for txt in texts[:150]:
            if not txt:
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
            page.wait_for_selector(r"text=/R\$\s?\d/", timeout=35000)
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
    print(f"▶ Flight Monitor v3.3 — {now_br.strftime('%d/%m/%Y %H:%M')} "
          f"(horário de Brasília)")
    print(f"  Janela: partidas de D+{MIN_DEP_OFFSET} a D+{SEARCH_WINDOW}  |  "
          f"Durações: {TRIP_DURATIONS} dias  |  Âncora fixa: D+{ANCHOR_OFFSET}")

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
        "window_start": MIN_DEP_OFFSET,
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
