#!/usr/bin/env python3
"""
Замер пропускной способности Steam Market с текущего IP.

Что делает:
  1. Показывает внешний IP и провайдера (чтобы сравнить точки между собой).
  2. Проверяет, какой HTTP-клиент Steam пропускает: urllib (как requests) или curl.
  3. Плавно поднимает частоту запросов к itemordershistogram, пока не придёт 429.
  4. Ждёт снятия ограничения и замеряет, сколько оно длится.
  5. Держит «безопасную» частоту SUSTAIN_MINUTES минут, чтобы подтвердить стабильность.
  6. Печатает отчёт и пересчёт в пользователей бота (опционально шлёт в Telegram).

Только стандартная библиотека Python + бинарник curl (если есть).
НЕ запускать на сервере, где с того же IP уже ходит в Steam другой бот.

Переменные окружения (все необязательные):
  MODE                  ip | check | full            (по умолчанию full)
  RATES                 ступени, запросов/мин        (6,10,15,20,30,45,60)
  STAGE_MINUTES         длительность ступени         (4)
  SUSTAIN_MINUTES       проверка стабильности        (20)
  MAX_RECOVERY_MINUTES  сколько ждать снятия лимита  (40)
  LOTS                  лотов на пользователя        (3)
  KEEP_ALIVE            1 = не завершаться после отчёта (для Northflank Service)
  TG_BOT_TOKEN, TG_CHAT_ID  прислать отчёт в Telegram
  LABEL                 имя точки в отчёте, например nf-1 / oracle-a1
"""
import json
import os
import random
import re
import shutil
import subprocess
import sys
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
NAMEID_RE = re.compile(r"Market_LoadOrderSpread\(\s*(\d+)\s*\)")


def env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


MODE = os.getenv("MODE", "full").strip().lower()
RATES = [float(x) for x in os.getenv("RATES", "6,10,15,20,30,45,60").split(",") if x.strip()]
STAGE_MIN = env_float("STAGE_MINUTES", 4)
SUSTAIN_MIN = env_float("SUSTAIN_MINUTES", 20)
MAX_RECOVERY_MIN = env_float("MAX_RECOVERY_MINUTES", 40)
LOTS = int(env_float("LOTS", 3))
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "0") == "1"
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")
LABEL = os.getenv("LABEL", "")

T0 = time.time()
CURL = shutil.which("curl")

# Всё, что попадёт в отчёт; заполняется по ходу, чтобы при остановке был частичный отчёт.
R = {"label": LABEL, "mode": MODE, "stages": [], "sustain": []}


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


def listing_url(name):
    return f"https://steamcommunity.com/market/listings/{APPID}/{urllib.parse.quote(name)}"


def histogram_url(nameid):
    q = urllib.parse.urlencode({"country": "US", "language": "english", "currency": 1,
                                "item_nameid": nameid, "two_factor": 0})
    return f"https://steamcommunity.com/market/itemordershistogram?{q}"


def classify_listing(status, body):
    if status == 429 or (status == 200 and body.strip() in ("", "null")):
        return "limit"
    if status == 200 and NAMEID_RE.search(body):
        return "ok"
    return "error" if status else "neterr"


def classify_hist(status, body):
    if status == 429:
        return "limit"
    if status == 200:
        try:
            data = json.loads(body)
        except ValueError:
            return "error"
        if data is None:  # Steam при ограничении отвечает телом null
            return "limit"
        return "ok" if isinstance(data, dict) and data.get("success") == 1 else "error"
    return "error" if status else "neterr"


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
    """Одна загрузка страницы предмета каждым клиентом. Возвращает (fetch, имя, nameid)."""
    url = listing_url(ITEMS[0])
    checks = {}

    s, b, lat = fetch_urllib(url)
    checks["urllib"] = {"status": s, "result": classify_listing(s, b), "latency": round(lat, 2)}
    log(f"urllib: HTTP {s} -> {checks['urllib']['result']}")
    nameid = NAMEID_RE.search(b).group(1) if checks["urllib"]["result"] == "ok" else None

    if CURL:
        time.sleep(6)
        s, b, lat = fetch_curl(url)
        checks["curl"] = {"status": s, "result": classify_listing(s, b), "latency": round(lat, 2)}
        log(f"curl:   HTTP {s} -> {checks['curl']['result']}")
        if checks["curl"]["result"] == "ok":
            nameid = NAMEID_RE.search(b).group(1)
    else:
        checks["curl"] = {"result": "not installed"}
        log("curl не установлен, проверяю только urllib")

    R["clients"] = checks
    if checks["curl"].get("result") == "ok":
        return fetch_curl, "curl", nameid
    if checks["urllib"]["result"] == "ok":
        return fetch_urllib, "urllib", nameid
    return None, None, None


def resolve_ids(fetch, first_id):
    ids = {ITEMS[0]: first_id}
    for name in ITEMS[1:]:
        time.sleep(8)
        for attempt in range(3):
            s, b, _ = fetch(listing_url(name))
            kind = classify_listing(s, b)
            if kind == "ok":
                ids[name] = NAMEID_RE.search(b).group(1)
                break
            if kind == "limit":
                log(f"429 уже при загрузке страниц предметов, жду 5 мин (попытка {attempt + 1}/3)")
                R.setdefault("notes", []).append("429 во время загрузки item_nameid")
                time.sleep(300)
            else:
                log(f"не удалось получить item_nameid для {name}: HTTP {s}")
                break
    log(f"item_nameid получены для {len(ids)} предметов")
    return list(ids.values())


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


def run_rate(fetch, ids, rate, minutes, meter):
    interval = 60.0 / rate
    start = time.time()
    end = start + minutes * 60
    n = ok = err = 0
    lats = []
    next_t = start
    i = random.randrange(len(ids))
    while time.time() < end:
        now = time.time()
        if now < next_t:
            time.sleep(next_t - now)
        s, b, lat = fetch(histogram_url(ids[i % len(ids)]))
        i += 1
        meter.add()
        n += 1
        kind = classify_hist(s, b)
        if kind == "limit":
            elapsed = time.time() - start
            return "limit", {"rate": rate, "requests": n, "ok": ok, "errors": err,
                             "seconds_until_429": round(elapsed), "http": s,
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
    elapsed = time.time() - start
    return "ok", {"rate": rate, "requests": n, "ok": ok, "errors": err,
                  "actual_rate": round(n / elapsed * 60, 1),
                  "avg_latency": round(sum(lats) / len(lats), 2) if lats else None}


def recover(fetch, nameid, meter):
    """Редкие пробы с растущей паузой: частые повторы только продлевают ограничение."""
    waited = 0.0
    for pause in [2, 4, 8, 15, 15, 15, 15, 15]:
        if waited + pause > MAX_RECOVERY_MIN:
            break
        log(f"  жду {pause} мин и пробую снова (всего ждём {waited + pause:.0f} мин)")
        time.sleep(pause * 60)
        waited += pause
        s, b, _ = fetch(histogram_url(nameid))
        meter.add()
        if classify_hist(s, b) == "ok":
            log(f"  ограничение снято, прошло не больше {waited:.0f} мин")
            return waited
    log(f"  за {MAX_RECOVERY_MIN:.0f} мин ограничение не снялось")
    return None


# ---------------------------------------------------------------- отчёт

def build_report():
    lines = [f"=== Steam capacity: {LABEL or R.get('ip', {}).get('ip', '?')} ==="]
    ipd = R.get("ip", {})
    lines.append(f"IP: {ipd.get('ip')} | {ipd.get('org')} | {ipd.get('city')}, {ipd.get('country')}")
    if "clients" in R:
        c = R["clients"]
        lines.append(f"urllib: {c['urllib'].get('result')} | curl: {c['curl'].get('result')}"
                     f" | выбран: {R.get('client')}")
    for st in R["stages"]:
        if st["result"] == "ok":
            lines.append(f"  ступень {st['rate']:g}/мин: OK ({st['requests']} запр., "
                         f"факт {st['actual_rate']}/мин, задержка {st['avg_latency']}с)")
        elif st["result"] == "limit":
            w = st["windows_at_429"]
            lines.append(f"  ступень {st['rate']:g}/мин: 429 через {st['seconds_until_429']}с "
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
        lines.append(f"Пользователей на эту точку ({LOTS} лота, реалистично ×2 за совпадения):")
        for interval in (15, 30, 60):
            pess = per_hour / (LOTS * 60 / interval)
            lines.append(f"  раз в {interval} мин: {pess:.0f} (пессим.) … {pess * 2:.0f} (реалист.)")
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
    log(f"режим {MODE}, ступени {RATES}, ступень {STAGE_MIN:g} мин, удержание {SUSTAIN_MIN:g} мин")
    R["ip"] = ip_info()
    log(f"внешний IP: {R['ip']}")
    if MODE == "ip":
        return

    fetch, client, first_id = choose_client()
    R["client"] = client
    if not fetch:
        R["notes"] = ["Ни один клиент не прошёл: IP уже ограничен или клиенты заблокированы"]
        log("Steam не пропустил ни один клиент. Подожди 30–60 мин и запусти снова.")
        return
    if MODE == "check":
        return

    ids = resolve_ids(fetch, first_id)
    meter = Meter()
    last_ok = None
    limit_hit = False

    for rate in RATES:
        log(f"ступень {rate:g} запр/мин на {STAGE_MIN:g} мин")
        res, st = run_rate(fetch, ids, rate, STAGE_MIN, meter)
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
        R["recovery_min"] = recover(fetch, ids[0], meter)
        if R["recovery_min"] is None:
            return
        sustain_rate = (last_ok if last_ok else RATES[0] / 2) * 0.75
    else:
        if last_ok is None:
            return
        sustain_rate = last_ok
        R["limit_not_reached"] = True

    for attempt in range(2):
        log(f"удержание {sustain_rate:.1f} запр/мин на {SUSTAIN_MIN:g} мин (попытка {attempt + 1}/2)")
        res, st = run_rate(fetch, ids, sustain_rate, SUSTAIN_MIN, meter)
        st["result"] = res
        R["sustain"].append(st)
        if res == "ok":
            R["safe_rate"] = sustain_rate
            log("  стабильно")
            break
        if res == "limit":
            log(f"  429 на удержании через {st['seconds_until_429']}с")
            R["recovery_min_sustain"] = recover(fetch, ids[0], meter)
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
