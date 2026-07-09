"""
Cruise Price Monitor — MSC Grand Voyages 2027
Monitora 3 cruzeiros transatlânticos específicos no site da MSC.
Roda 1× por dia junto ao flight monitor via GitHub Actions.
Alerta no Telegram quando preço cair abaixo do mínimo histórico.
"""

import re
import json
import os
import time
import random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ─── Credenciais ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
OUTPUT_DIR       = os.environ.get("OUTPUT_DIR", "docs")

TZ_BR = ZoneInfo("America/Sao_Paulo")

# ─── Cruzeiros monitorados ────────────────────────────────────────────────────
# URL base: sem o parâmetro 'src' (token de sessão que muda a cada visita)
CRUISES = [
    {
        "id":       "divina",
        "name":     "MSC Divina",
        "route":    "Santos → Barcelona",
        "date_fmt": "14 Mar 2027",
        "nights":   16,
        "cabin":    "Cabine Interna",
        "url": (
            "https://www.msccruzeiros.com.br/Booking"
            "?CruiseID=DI20270314SSZBCN"
            "&Type=CROL"
            "&MacroCategory=INS"
            "&Category=IB"
            "&NewCruise=true"
            "#/pricing"
        ),
    },
    {
        "id":       "musica",
        "name":     "MSC Musica",
        "route":    "Santos → Tarragona",
        "date_fmt": "01 Abr 2027",
        "nights":   14,
        "cabin":    "Cabine Externa",
        "url": (
            "https://www.msccruzeiros.com.br/Booking"
            "?CruiseID=MU20270401SSZTAR"
            "&Type=CROL"
            "&MacroCategory=OUT"
            "&Category=OB"
            "&NewCruise=true"
            "#/pricing"
        ),
    },
    {
        "id":       "virtuosa",
        "name":     "MSC Virtuosa",
        "route":    "Rio de Janeiro → Barcelona",
        "date_fmt": "04 Abr 2027",
        "nights":   16,
        "cabin":    "Cabine Interna",
        "url": (
            "https://www.msccruzeiros.com.br/Booking"
            "?CruiseID=VI20270403SSZBCN"
            "&Type=CROL"
            "&MacroCategory=INS"
            "&Category=IB"
            "&NewCruise=true"
            "#/pricing"
        ),
    },
]

# Regex: captura preços no formato brasileiro (R$ 3.809 ou R$ 3.809,00)
PRICE_RE = re.compile(r'R\$\s*([\d]{1,3}(?:\.[\d]{3})*(?:,\d{2})?)')


# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_brl(text):
    """Extrai preços BRL do texto. Retorna lista de floats."""
    prices = []
    for m in PRICE_RE.findall(text):
        try:
            # Remove separador de milhar e converte vírgula decimal
            v = float(m.replace('.', '').replace(',', '.'))
            # Filtro de faixa realista para cruzeiro (R$ 1.000 – R$ 80.000)
            if 1_000 <= v <= 80_000:
                prices.append(v)
        except ValueError:
            pass
    return prices


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
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"  [Telegram] {e}")


# ─── Scraper ──────────────────────────────────────────────────────────────────

def scrape_price(page, cruise):
    """
    Navega para a página de preços do cruzeiro e extrai o menor preço/pessoa.
    Retorna float ou None.
    """
    print(f"    abrindo: {cruise['url'][:80]}...")
    try:
        page.goto(cruise["url"], wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"    [erro ao navegar] {e}")
        return None

    # Aguarda algum preço aparecer na página (até 30s)
    try:
        page.wait_for_selector(r"text=/R\$\s*\d/", timeout=30000)
    except PWTimeout:
        body = page.inner_text("body").lower()
        if "login" in body or "entrar" in body:
            print("    [página pediu login]")
        else:
            print(f"    [sem preços na página]")
        # Diagnóstico
        try:
            page.screenshot(path=f"debug_cruise_{cruise['id']}.png", full_page=False)
        except Exception:
            pass
        return None

    # Espera a página terminar de renderizar
    time.sleep(3)

    # Extrai todos os preços da página
    try:
        body_text = page.inner_text("body")
    except Exception:
        return None

    prices = parse_brl(body_text)
    if not prices:
        print("    [nenhum preço válido na faixa esperada]")
        return None

    # O menor preço é o mais relevante (preço por pessoa mínimo)
    best = min(prices)
    print(f"    preços encontrados: {sorted(set(prices))[:8]}")
    print(f"    melhor: R$ {best:,.0f}/pessoa")
    return best


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    now_br    = datetime.now(TZ_BR)
    today_str = now_br.strftime("%Y-%m-%d")
    print(f"▶ Cruise Monitor — {now_br.strftime('%d/%m/%Y %H:%M')} (horário de Brasília)")

    hist_path = os.path.join(OUTPUT_DIR, "cruise_history.json")
    data_path = os.path.join(OUTPUT_DIR, "cruise_data.json")
    history   = load_json(hist_path, default={})

    results = []
    alerts  = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = browser.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )

        for cruise in CRUISES:
            print(f"\n  → {cruise['name']}  {cruise['route']}")
            page = ctx.new_page()
            page.set_default_timeout(20000)

            try:
                price = scrape_price(page, cruise)
            except Exception as e:
                print(f"    [erro geral] {e}")
                price = None
            finally:
                page.close()

            if price is None:
                print(f"    sem resultado")
                time.sleep(random.uniform(5, 9))
                continue

            cid  = cruise["id"]
            hist = history.get(cid, [])

            # Histórico: guarda o menor preço do dia
            entry = next((h for h in hist if h["date"] == today_str), None)
            if entry:
                if price < entry["price"]:
                    entry["price"] = price
            else:
                hist.append({"date": today_str, "price": price})
            history[cid] = hist

            # Menor preço já registrado (exceto hoje → não compara consigo mesmo)
            prev_prices = [h["price"] for h in hist if h["date"] != today_str]
            min_ever    = min(prev_prices) if prev_prices else price
            pct         = round((price - min_ever) / min_ever * 100, 1) if prev_prices else 0.0
            is_new_min  = bool(prev_prices) and price < min_ever

            result = {
                "id":         cid,
                "name":       cruise["name"],
                "route":      cruise["route"],
                "date_fmt":   cruise["date_fmt"],
                "nights":     cruise["nights"],
                "cabin":      cruise["cabin"],
                "price":      round(price),
                "min_ever":   round(min_ever),
                "pct_vs_min": pct,
                "is_new_min": is_new_min,
                "n_points":   len(hist),
                "url":        cruise["url"],
            }
            results.append(result)

            status = "🔽 NOVA MÍNIMA" if is_new_min else ("↑ acima" if pct > 0 else "→ igual")
            print(f"    R$ {price:,.0f}/pessoa  |  mín. histórica: R$ {min_ever:,.0f}  |  {pct:+.1f}%  {status}")

            # Alerta Telegram quando preço cai abaixo do mínimo histórico
            if is_new_min:
                alerts.append(
                    f"🛳️ <b>CRUZEIRO MAIS BARATO!</b>\n\n"
                    f"<b>{cruise['name']}</b> — {cruise['route']}\n"
                    f"📅 {cruise['date_fmt']} · {cruise['nights']} noites · {cruise['cabin']}\n\n"
                    f"💰 Preço atual: <b>R$ {price:,.0f}/pessoa</b>\n"
                    f"📊 Mínima anterior: R$ {min_ever:,.0f}\n"
                    f"📉 Queda: <b>{pct:.1f}%</b>\n\n"
                    f"🔗 <a href='{cruise['url']}'>Reservar na MSC</a>"
                )

            time.sleep(random.uniform(5, 10))

        ctx.close()
        browser.close()

    # Ordenar por preço atual
    results.sort(key=lambda r: r["price"])

    payload = {
        "updated_at":  datetime.now(timezone.utc).isoformat(),
        "updated_fmt": now_br.strftime("%d/%m/%Y %H:%M"),
        "cruises":     results,
    }
    save_json(data_path, payload)
    save_json(hist_path, history)
    print(f"\n✓ {len(results)}/3 cruzeiros salvos em {OUTPUT_DIR}/")

    for msg in alerts:
        send_telegram(msg)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
