#!/usr/bin/env python3
"""
Замер пропускной способности Steam Market с текущего IP.

Что делает:
  1. Показывает внешний IP и провайдера (чтобы сравнить точки между собой).
  2. Проверяет, какой HTTP-клиент Steam пропускает: urllib (как requests) или curl.
  3. Плавно поднимает частоту запросов к выбранному эндпоинту, пока не придёт 429.
  4. Ждёт снятия ограничения и замеряет, сколько оно длится.
  5. Держит «безопасную» частоту SUSTAIN_MINUTES минут, чтобы подтвердить стабильность.
  6. Печатает отчёт и пересчёт в пользователей бота (опционально шлёт в Telegram).

Только стандартная библиотека Python + бинарник curl (если есть).
НЕ запускать на сервере, где с того же IP уже ходит в Steam другой бот.

Переменные окружения (все необязательные):
  MODE                  ip | check | full | simulate (по умолчанию full)
  ENDPOINT              search | priceoverview | mixed (search; mixed = по очереди оба)
  RATES                 ступени, запросов/мин        (6,10,15,20,30,45,60)
  STAGE_MINUTES         длительность ступени         (4)
  SUSTAIN_MINUTES       проверка стабильности        (20)
  MAX_RECOVERY_MINUTES  сколько ждать снятия лимита  (40)
  LOTS                  лотов на пользователя        (3)
  KEEP_ALIVE            1 = не завершаться после отчёта (для Northflank Service)
  TG_BOT_TOKEN, TG_CHAT_ID  прислать отчёт в Telegram
  LABEL                 имя точки в отчёте, например nf-1 / oracle-a1

Режим simulate — ведёт себя как будущий воркер бота и меряет реальную отдачу:
  частота по каждому эндпоинту своя; при 429 эндпоинт уходит на паузу COOLDOWN_MINUTES
  и частота ×0.7; каждые RAISE_EVERY_MINUTES без 429 частота +RAISE_STEP (до MAX_RATE).
  SIM_MINUTES           длительность                 (60)
  START_RATE            стартовая частота, в мин     (8)
  MAX_RATE / MIN_RATE   границы частоты              (14 / 2)
  COOLDOWN_MINUTES      пауза после 429              (3)
  RAISE_EVERY_MINUTES   как часто ускоряться         (5)
  RAISE_STEP            шаг ускорения                (1)

Эндпоинты:
  search         /market/search/render?norender=1 — sell_listings и sell_price, один запрос на предмет.
  priceoverview  /market/priceoverview — lowest_price и volume.
(Страницы /market/listings/ с сентября 2026 на новом движке и item_nameid не содержат,
 поэтому itemordershistogram больше не используется.)
"""
import json
import os
import random
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

APPID = 730
ITEMS = [
    "AK-47 | Redline (Field-Tested)",
    "AWP | Asiimov (Field-Tested)",
    "M4A1-S | Hyper Beast (Field-Tested)",
    "Glock-18 | Water Elemental (Field-Tested)",
    "Desert Eagle | Printstream (Field-Tested)",
    "Kilowatt Case",
    "Revolution Case",
    "Fracture Case",
]
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


MODE = os.getenv("MODE", "full").strip().lower()
ENDPOINT = os.getenv("ENDPOINT", "search").strip().lower()
RATES = [float(x) for x in os.getenv("RATES", "6,10,15,20,30,45,60").split(",") if x.strip()]
STAGE_MIN = env_float("STAGE_MINUTES", 4)
SUSTAIN_MIN = env_float("SUSTAIN_MINUTES", 20)
MAX_RECOVERY_MIN = env_float("MAX_RECOVERY_MINUTES", 40)
LOTS = int(env_float("LOTS", 3))
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "0") == "1"
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")
LABEL = os.getenv("LABEL", "")
SIM_MIN = env_float("SIM_MINUTES", 60)
START_RATE = env_float("START_RATE", 8)
MAX_RATE = env_float("MAX_RATE", 14)
MIN_RATE = env_float("MIN_RATE", 2)
COOLDOWN_MIN = env_float("COOLDOWN_MINUTES", 3)
RAISE_EVERY_MIN = env_float("RAISE_EVERY_MINUTES", 5)
RAISE_STEP = env_float("RAISE_STEP", 1)

T0 = time.time()
CURL = shutil.which("curl")

# Всё, что попадёт в отчёт; заполняется по ходу, чтобы при остановке был частичный отчёт.
R = {"label": LABEL, "mode": MODE, "endpoint": ENDPOINT, "stages": [], "sustain": []}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')} +{(time.time() - T0) / 60:5.1f}m] {msg}", flush=True)


# ---------------------------------------------------------------- HTTP

def fetch_urllib(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.time() - t
    except Exception as e:  # сеть, таймаут, TLS
        return 0, repr(e), time.time() - t


def fetch_curl(url, timeout=20):
    # Параметры curl по умолчанию: именно такой вызов Steam пропускал с Oracle.
    t = time.time()
    try:
        p = subprocess.run([CURL, "-s", "-m", str(timeout), "-w", "\n%{http_code}", url],
                           capture_output=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return 0, "timeout", time.time() - t
    body, _, code = p.stdout.decode("utf-8", "replace").rpartition("\n")
    try:
        status = int(code)
    except ValueError:
        status = 0
    return status, body, time.time() - t


def endpoint_url(name, kind=None):
    if (kind or ENDPOINT) == "priceoverview":
        q = urllib.parse.urlencode({"appid": APPID, "currency": 1, "market_hash_name": name})
        return f"https://steamcommunity.com/market/priceoverview/?{q}"
    q = urllib.parse.urlencode({"query": name, "appid": APPID, "norender": 1, "count": 10,
                                "search_descriptions": 0})
    return f"https://steamcommunity.com/market/search/render/?{q}"


def classify(status, body):
    if status == 429:
        return "limit"
    if status == 200:
        try:
            data = json.loads(body)
        except ValueError:
            return "error"
        if data is None:  # Steam при ограничении отвечает телом null
            return "limit"
        return "ok" if isinstance(data, dict) and data.get("success") in (True, 1) else "error"
    return "error" if status else "neterr"


def describe(body, name):
    """Короткая выжимка ответа для лога: убеждаемся, что данные настоящие."""
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    if "results" not in data:
        return f"lowest={data.get('lowest_price')} volume={data.get('volume')}"
    for item in data.get("results") or []:
        if item.get("hash_name") == name:
            return f"sell_listings={item.get('sell_listings')} price={item.get('sell_price_text')}"
    return f"точного совпадения нет, результатов {data.get('total_count')}"


# ---------------------------------------------------------------- шаги

def ip_info():
    status, body, _ = fetch_urllib("https://ipinfo.io/json")
    if status == 200:
        try:
            d = json.loads(body)
            return {k: d.get(k) for k in ("ip", "city", "region", "country", "org")}
        except ValueError:
            pass
    status, body, _ = fetch_urllib("https://api.ipify.org")
    return {"ip": body.strip() if status == 200 else None}


def choose_client():
    """По одному запросу каждым клиентом. Возвращает (fetch, имя)."""
    name = ITEMS[0]
    url = endpoint_url(name)
    checks = {}

    s, b, lat = fetch_urllib(url)
    checks["urllib"] = {"status": s, "result": classify(s, b), "latency": round(lat, 2)}
    log(f"urllib: HTTP {s} -> {checks['urllib']['result']} {describe(b, name)}")
    if checks["urllib"]["result"] == "error":
        log(f"  тело: {b[:200]!r}")

    if CURL:
        time.sleep(6)
        s, b, lat = fetch_curl(url)
        checks["curl"] = {"status": s, "result": classify(s, b), "latency": round(lat, 2)}
        log(f"curl:   HTTP {s} -> {checks['curl']['result']} {describe(b, name)}")
        if checks["curl"]["result"] == "error":
            log(f"  тело: {b[:200]!r}")
    else:
        checks["curl"] = {"result": "not installed"}
        log("curl не установлен, проверяю только urllib")

    R["clients"] = checks
    if checks["curl"].get("result") == "ok":
        return fetch_curl, "curl"
    if checks["urllib"]["result"] == "ok":
        return fetch_urllib, "urllib"
    return None, None


class Meter:
    """Скользящие окна запросов: сколько было за последние 1/5/10/60 минут."""

    def __init__(self):
        self.times = deque()

    def add(self):
        now = time.time()
        self.times.append(now)
        while self.times and self.times[0] < now - 3600:
            self.times.popleft()

    def window(self, sec):
        edge = time.time() - sec
        return sum(1 for t in self.times if t >= edge)

    def snapshot(self):
        return {"last_1m": self.window(60), "last_5m": self.window(300),
                "last_10m": self.window(600), "last_60m": self.window(3600)}


def run_rate(fetch, rate, minutes, meter):
    interval = 60.0 / rate
    start = time.time()
    end = start + minutes * 60
    n = ok = err = 0
    lats = []
    next_t = start
    i = random.randrange(len(ITEMS))
    while time.time() < end:
        now = time.time()
        if now < next_t:
            time.sleep(next_t - now)
        # mixed: по очереди search и priceoverview — проверяем, общий ли у них лимит
        ep = ("search" if i % 2 == 0 else "priceoverview") if ENDPOINT == "mixed" else ENDPOINT
        s, b, lat = fetch(endpoint_url(ITEMS[i % len(ITEMS)], ep))
        i += 1
        meter.add()
        n += 1
        kind = classify(s, b)
        if kind == "limit":
            elapsed = time.time() - start
            return "limit", {"rate": rate, "requests": n, "ok": ok, "errors": err,
                             "seconds_until_429": round(elapsed), "http": s, "endpoint_at_429": ep,
                             "windows_at_429": meter.snapshot()}
        if kind == "ok":
            ok += 1
            lats.append(lat)
        else:
            err += 1
            log(f"  ошибка HTTP {s}: {b[:120]!r}")
            if n >= 5 and err > n / 2:
                return "error", {"rate": rate, "requests": n, "ok": ok, "errors": err}
        next_t += interval * random.uniform(0.85, 1.15)
        next_t = max(next_t, time.time())  # не догоняем пачкой, если отстали
    elapsed = max(time.time() - start, 1e-6)
    return "ok", {"rate": rate, "requests": n, "ok": ok, "errors": err,
                  "actual_rate": round(n / elapsed * 60, 1),
                  "avg_latency": round(sum(lats) / len(lats), 2) if lats else None}


def recover(fetch, meter):
    """Редкие пробы с растущей паузой: частые повторы только продлевают ограничение."""
    waited = 0.0
    for pause in [2, 4, 8, 15, 15, 15, 15, 15]:
        if waited + pause > MAX_RECOVERY_MIN:
            break
        log(f"  жду {pause} мин и пробую снова (всего ждём {waited + pause:.0f} мин)")
        time.sleep(pause * 60)
        waited += pause
        s, b, _ = fetch(endpoint_url(ITEMS[0]))
        meter.add()
        if classify(s, b) == "ok":
            log(f"  ограничение снято, прошло не больше {waited:.0f} мин")
            return waited
    log(f"  за {MAX_RECOVERY_MIN:.0f} мин ограничение не снялось")
    return None


def simulate(fetch):
    """Работа как у настоящего воркера: не останавливаемся на 429, а притормаживаем и продолжаем."""
    eps = ["search", "priceoverview"] if ENDPOINT == "mixed" else [ENDPOINT]
    start = time.time()
    end = start + SIM_MIN * 60
    st = {ep: {"rate": START_RATE, "next": start + k * 3, "cool_until": 0.0, "last_change": start,
               "ok": 0, "limit": 0, "err": 0, "err_row": 0, "cool_sec": 0.0,
               "i": random.randrange(len(ITEMS))}
          for k, ep in enumerate(eps)}
    buckets = []  # по 10 минут: {ep: {"ok": n, "limit": n}}
    R["sim"] = {"config": {"minutes": SIM_MIN, "start_rate": START_RATE, "max_rate": MAX_RATE,
                           "min_rate": MIN_RATE, "cooldown_min": COOLDOWN_MIN,
                           "raise_every_min": RAISE_EVERY_MIN, "raise_step": RAISE_STEP},
                "endpoints": {}, "buckets": buckets}
    next_summary = start + 300

    def publish():
        elapsed_h = max(time.time() - start, 1e-6) / 3600
        R["sim"]["elapsed_min"] = round(elapsed_h * 60, 1)
        for ep, s in st.items():
            R["sim"]["endpoints"][ep] = {
                "ok": s["ok"], "limit_429": s["limit"], "errors": s["err"],
                "cooldown_min": round(s["cool_sec"] / 60, 1), "final_rate": round(s["rate"], 1),
                "ok_per_hour": round(s["ok"] / elapsed_h)}
        R["sim"]["ok_per_hour_total"] = sum(e["ok_per_hour"] for e in R["sim"]["endpoints"].values())

    log(f"simulate: {', '.join(eps)}, старт {START_RATE:g}/мин на эндпоинт, потолок {MAX_RATE:g}, "
        f"{SIM_MIN:g} мин")
    try:
        while True:
            now = time.time()
            for ep, s in st.items():
                if (now >= s["cool_until"] and s["rate"] < MAX_RATE
                        and now - s["last_change"] >= RAISE_EVERY_MIN * 60):
                    s["rate"] = min(MAX_RATE, s["rate"] + RAISE_STEP)
                    s["last_change"] = now
                    log(f"  {ep}: {RAISE_EVERY_MIN:g} мин без 429 -> {s['rate']:.1f}/мин")

            ep = min(st, key=lambda e: max(st[e]["next"], st[e]["cool_until"]))
            s = st[ep]
            wake = max(s["next"], s["cool_until"])
            if wake >= end:
                break
            if wake > now:
                time.sleep(wake - now)
                continue  # пересчитать ускорения и выбор эндпоинта после сна

            code, body, _ = fetch(endpoint_url(ITEMS[s["i"] % len(ITEMS)], ep))
            s["i"] += 1
            t = time.time()
            idx = int((t - start) // 600)
            while len(buckets) <= idx:
                buckets.append({e: {"ok": 0, "limit": 0} for e in eps})
            kind = classify(code, body)
            if kind == "ok":
                s["ok"] += 1
                s["err_row"] = 0
                buckets[idx][ep]["ok"] += 1
                s["next"] = t + 60.0 / s["rate"] * random.uniform(0.85, 1.15)
            elif kind == "limit":
                s["limit"] += 1
                buckets[idx][ep]["limit"] += 1
                old = s["rate"]
                s["rate"] = max(MIN_RATE, s["rate"] * 0.7)
                s["cool_until"] = t + COOLDOWN_MIN * 60
                s["cool_sec"] += min(COOLDOWN_MIN * 60, max(end - t, 0))
                s["last_change"] = s["cool_until"]
                s["next"] = s["cool_until"]
                log(f"  {ep}: 429 -> пауза {COOLDOWN_MIN:g} мин, частота {old:.1f} -> {s['rate']:.1f}/мин")
            else:
                s["err"] += 1
                s["err_row"] += 1
                s["next"] = t + 60.0 / s["rate"]
                log(f"  {ep}: ошибка HTTP {code}: {body[:120]!r}")
                if s["err_row"] >= 10:
                    log(f"  {ep}: 10 ошибок подряд — пауза 5 мин")
                    s["cool_until"] = t + 300
                    s["err_row"] = 0

            if t >= next_summary:
                next_summary += 300
                publish()
                parts = [f"{e}: {v['ok']} ok, 429×{v['limit_429']}, {st[e]['rate']:.1f}/мин"
                         for e, v in R["sim"]["endpoints"].items()]
                log(f"итог за {R['sim']['elapsed_min']:g} мин — " + " | ".join(parts))
    finally:
        publish()


def user_capacity_lines(per_hour):
    lines = [f"Пользователей на эту точку ({LOTS} лота, реалистично ×2 за совпадения):"]
    for interval in (15, 30, 60):
        pess = per_hour / (LOTS * 60 / interval)
        lines.append(f"  раз в {interval} мин: {pess:.0f} (пессим.) … {pess * 2:.0f} (реалист.)")
    return lines


# ---------------------------------------------------------------- отчёт

def build_report():
    lines = [f"=== Steam capacity: {LABEL or R.get('ip', {}).get('ip', '?')} ({ENDPOINT}) ==="]
    ipd = R.get("ip", {})
    lines.append(f"IP: {ipd.get('ip')} | {ipd.get('org')} | {ipd.get('city')}, {ipd.get('country')}")
    ip_end = R.get("ip_end", {}).get("ip")
    if ip_end and ip_end != ipd.get("ip"):
        lines.append(f"ВНИМАНИЕ: IP сменился во время теста -> {ip_end}, результаты неточные")
    if "clients" in R:
        c = R["clients"]
        lines.append(f"urllib: {c['urllib'].get('result')} (HTTP {c['urllib'].get('status')}) | "
                     f"curl: {c['curl'].get('result')} (HTTP {c['curl'].get('status')}) | "
                     f"выбран: {R.get('client')}")
    for st in R["stages"]:
        if st["result"] == "ok":
            lines.append(f"  ступень {st['rate']:g}/мин: OK ({st['requests']} запр., "
                         f"факт {st['actual_rate']}/мин, задержка {st['avg_latency']}с)")
        elif st["result"] == "limit":
            w = st["windows_at_429"]
            lines.append(f"  ступень {st['rate']:g}/мин: 429 ({st.get('endpoint_at_429')}) "
                         f"через {st['seconds_until_429']}с "
                         f"(за 1м {w['last_1m']}, 5м {w['last_5m']}, 10м {w['last_10m']}, "
                         f"60м {w['last_60m']} запр.)")
        else:
            lines.append(f"  ступень {st['rate']:g}/мин: ошибки ({st['errors']}/{st['requests']})")
    if "recovery_min" in R:
        rm = R["recovery_min"]
        lines.append(f"Снятие ограничения: {'≤ %g мин' % rm if rm is not None else 'не дождались'}")
    for s in R["sustain"]:
        res = "стабильно" if s["result"] == "ok" else "429" if s["result"] == "limit" else "ошибки"
        lines.append(f"  удержание {s['rate']:.1f}/мин × {SUSTAIN_MIN:g} мин: {res}")

    safe = R.get("safe_rate")
    if safe:
        per_hour = safe * 60
        lines.append(f"БЕЗОПАСНО: {safe:.1f} запр/мин ≈ {per_hour:.0f} запр/час")
        if R.get("limit_not_reached"):
            lines.append("  (лимит не достигнут — реальный потолок выше)")
        lines += user_capacity_lines(per_hour)
    elif MODE == "simulate" and "sim" in R:
        sim = R["sim"]
        lines.append(f"Симуляция воркера: {sim.get('elapsed_min')} мин, старт {START_RATE:g}/мин, "
                     f"потолок {MAX_RATE:g}, пауза после 429 {COOLDOWN_MIN:g} мин")
        for ep, v in sim["endpoints"].items():
            lines.append(f"  {ep}: {v['ok']} успешных ({v['ok_per_hour']}/час), 429×{v['limit_429']}, "
                         f"ошибок {v['errors']}, на паузе {v['cooldown_min']} мин, "
                         f"частота в конце {v['final_rate']}/мин")
        lines.append("По 10 минут (успешные / 429):")
        for n, b in enumerate(sim["buckets"]):
            cells = " | ".join(f"{ep} {c['ok']}/{c['limit']}" for ep, c in b.items())
            lines.append(f"  {n * 10:>3}–{n * 10 + 10} мин: {cells}")
        total = sim.get("ok_per_hour_total", 0)
        lines.append(f"РЕАЛЬНАЯ ОТДАЧА: ≈ {total} успешных проверок в час")
        lines += user_capacity_lines(total)
    elif MODE == "full":
        lines.append("Безопасная частота не определена (см. ступени выше)")
    lines.append(f"Длительность теста: {(time.time() - T0) / 60:.0f} мин")
    return "\n".join(lines)


def send_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=15)
    except Exception as e:
        log(f"Telegram не отправлен: {e!r}")


def finish():
    if MODE != "ip":
        R["ip_end"] = ip_info()
    report = build_report()
    print("\n" + report, flush=True)
    R["duration_min"] = round((time.time() - T0) / 60)
    print("RESULT_JSON " + json.dumps(R, ensure_ascii=False), flush=True)
    send_telegram(report)
    if KEEP_ALIVE:
        log("KEEP_ALIVE=1: тест завершён, процесс просто спит (чтобы сервис не перезапускал тест)")
        while True:
            time.sleep(3600)


# ---------------------------------------------------------------- main

def main():
    if MODE == "simulate":
        log(f"режим simulate, эндпоинт {ENDPOINT}, {SIM_MIN:g} мин")
    else:
        log(f"режим {MODE}, эндпоинт {ENDPOINT}, ступени {RATES}, ступень {STAGE_MIN:g} мин, "
            f"удержание {SUSTAIN_MIN:g} мин")
    R["ip"] = ip_info()
    log(f"внешний IP: {R['ip']}")
    if MODE == "ip":
        return

    fetch, client = choose_client()
    R["client"] = client
    if not fetch:
        R["notes"] = ["Ни один клиент не прошёл: IP уже ограничен или клиенты заблокированы"]
        log("Steam не пропустил ни один клиент. Подожди 30–60 мин и запусти снова.")
        return
    if MODE == "check":
        return
    if MODE == "simulate":
        simulate(fetch)
        return

    meter = Meter()
    last_ok = None
    limit_hit = False

    for rate in RATES:
        log(f"ступень {rate:g} запр/мин на {STAGE_MIN:g} мин")
        res, st = run_rate(fetch, rate, STAGE_MIN, meter)
        st["result"] = res
        R["stages"].append(st)
        if res == "ok":
            last_ok = rate
            log(f"  OK: {st['requests']} запросов, факт {st['actual_rate']}/мин")
        elif res == "limit":
            limit_hit = True
            log(f"  429 на {rate:g}/мин через {st['seconds_until_429']}с, окна: {st['windows_at_429']}")
            break
        else:
            log("  слишком много ошибок, останавливаю подъём")
            break

    if limit_hit:
        R["recovery_min"] = recover(fetch, meter)
        if R["recovery_min"] is None:
            return
        sustain_rate = (last_ok if last_ok else RATES[0] / 2) * 0.75
    else:
        if last_ok is None:
            return
        sustain_rate = last_ok
        R["limit_not_reached"] = True

    if SUSTAIN_MIN <= 0:
        log("удержание отключено (SUSTAIN_MINUTES=0)")
        return

    for attempt in range(2):
        log(f"удержание {sustain_rate:.1f} запр/мин на {SUSTAIN_MIN:g} мин (попытка {attempt + 1}/2)")
        res, st = run_rate(fetch, sustain_rate, SUSTAIN_MIN, meter)
        st["result"] = res
        R["sustain"].append(st)
        if res == "ok":
            R["safe_rate"] = sustain_rate
            log("  стабильно")
            break
        if res == "limit":
            log(f"  429 на удержании через {st['seconds_until_429']}с")
            R["recovery_min_sustain"] = recover(fetch, meter)
            if R["recovery_min_sustain"] is None:
                break
            sustain_rate *= 0.6
        else:
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("остановлено вручную, печатаю частичный отчёт")
        R.setdefault("notes", []).append("остановлено вручную")
    finish()
