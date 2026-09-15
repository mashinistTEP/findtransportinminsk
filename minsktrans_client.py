"""
minsktrans_client.py — клиент lookout_yard API Минсктранса.
Эндпоинты восстановлены из реального трафика Chrome (PCAPdroid, 13-14.09.2026).
"""

import json
import logging
import os
import re
import time
import requests

log = logging.getLogger("minsktrans")

BASE = "https://minsktrans.by/lookout_yard"
SLEEP_BETWEEN_REQUESTS = 0.3

VEHICLE_TYPES = {
    "bus":     "bus",
    "trolley": "trolleybus",
    "tram":    "tram",
}

SCOREBOARD_TYPE_LETTER = {
    "bus":     "А",
    "trolley": "Т",
    "tram":    "#",
}

ROUTES_CACHE_FILE = "routes_cache.json"


class MinsktransClient:
    def __init__(self, place="minsk"):
        self.place = place
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE}/Home/Index/{place}",
        })
        self.token = None
        self._bootstrap()

    def _bootstrap(self):
        log.info("Bootstrap: получаю CSRF-токен с сайта…")
        resp = self.session.get(f"{BASE}/Home/Index/{self.place}", timeout=20)
        resp.raise_for_status()

        # пробуем несколько вариантов разметки — сайт мог слегка измениться
        patterns = [
            r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
            r'value="([^"]+)"[^>]*name="__RequestVerificationToken"',
            r'"__RequestVerificationToken"\s*:\s*"([^"]+)"',
        ]
        for p in patterns:
            m = re.search(p, resp.text)
            if m:
                self.token = m.group(1)
                log.info("Bootstrap OK, токен получен (len=%d)", len(self.token))
                return

        # если ни один не сработал — показываем кусок страницы для диагностики
        log.error("Bootstrap FAIL: не найден __RequestVerificationToken")
        log.error("Начало страницы (первые 500 символов):\n%s", resp.text[:500])
        raise RuntimeError("Не нашёл __RequestVerificationToken — см. логи")

    def _post(self, endpoint, data, retry=True):
        payload = dict(data)
        payload["__RequestVerificationToken"] = self.token
        try:
            resp = self.session.post(
                f"{BASE}/Data/{endpoint}", data=payload, timeout=20
            )
        except requests.RequestException as e:
            log.warning("POST %s сетевая ошибка: %s", endpoint, e)
            raise
        if resp.status_code in (400, 403) and retry:
            log.warning("POST %s → %d, обновляю токен", endpoint, resp.status_code)
            self._bootstrap()
            return self._post(endpoint, data, retry=False)
        if not resp.ok:
            log.warning("POST %s → HTTP %d: %s", endpoint, resp.status_code, resp.text[:200])
            resp.raise_for_status()
        time.sleep(SLEEP_BETWEEN_REQUESTS)
        try:
            return resp.json()
        except Exception:
            log.error("POST %s: не удалось разобрать JSON: %s", endpoint, resp.text[:300])
            raise

    def get_scoreboard(self, stop_id):
        return self._post("Scoreboard", {
            "p": self.place, "s": stop_id, "v": int(time.time() * 1000),
        })

    def get_route(self, vtype, route_number):
        return self._post("Route", {
            "p": self.place, "tt": VEHICLE_TYPES[vtype], "r": route_number,
        })

    def get_vehicles(self, vtype, route_number):
        return self._post("Vehicles", {
            "p": self.place, "tt": VEHICLE_TYPES[vtype],
            "r": route_number, "v": int(time.time() * 1000),
        })


def discover_routes(client, vtype, max_number=150):
    """Обходим номера 1..max_number, оставляем те, у которых есть остановки."""
    routes = {}
    log.info("discover_routes [%s]: проверяю номера 1..%d", vtype, max_number)

    # --- диагностика: смотрим что реально возвращает /Data/Route для маршрута 1 ---
    try:
        sample = client.get_route(vtype, 1)
        log.info("ДИАГНОСТИКА Route[%s][1] raw keys: %s", vtype, list(sample.keys()))
        trips_sample = sample.get("Trips") or sample.get("trips") or {}
        log.info("ДИАГНОСТИКА Trips keys: %s", list(trips_sample.keys()) if trips_sample else "нет Trips")
    except Exception as e:
        log.warning("ДИАГНОСТИКА Route[%s][1] exception: %s", vtype, e)

    for n in range(1, max_number + 1):
        try:
            data = client.get_route(vtype, n)
        except Exception as e:
            log.debug("Route[%s][%d] ошибка: %s", vtype, n, e)
            continue

        # пробуем оба регистра ключа на случай расхождения
        trips = data.get("Trips") or data.get("trips")
        if not trips:
            continue

        stops_a = trips.get("StopsA") or trips.get("stopsA") or []
        stops_b = trips.get("StopsB") or trips.get("stopsB") or []
        if not stops_a and not stops_b:
            continue

        routes[str(n)] = trips
        log.debug("Route[%s][%d] найден: StopsA=%d StopsB=%d",
                  vtype, n, len(stops_a), len(stops_b))

    log.info("discover_routes [%s]: итого %d маршрутов", vtype, len(routes))
    return routes


def load_routes_cache(path=ROUTES_CACHE_FILE):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Не удалось прочитать кэш %s: %s", path, e)
    return {}


def save_routes_cache(cache, path=ROUTES_CACHE_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        log.warning("Не удалось сохранить кэш %s: %s", path, e)


def get_or_build_routes(client, vtype, cache_path=ROUTES_CACHE_FILE):
    cache = load_routes_cache(cache_path)
    if vtype not in cache or not cache[vtype]:
        log.info("Кэш для [%s] пуст или отсутствует — строю заново", vtype)
        cache[vtype] = discover_routes(client, vtype)
        save_routes_cache(cache, cache_path)
    else:
        log.info("Кэш для [%s]: %d маршрутов (с диска)", vtype, len(cache[vtype]))
    return cache[vtype]


def _haversine_m(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, asin, sqrt
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1; dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return 2 * 6371000 * asin(sqrt(a))


def _nearest_stop(trips, lat, lon):
    best = None
    for stops_key, names_key, dkey in (
        ("StopsA", "StopNamesA", "A"), ("StopsB", "StopNamesB", "B")
    ):
        stops = trips.get(stops_key) or []
        names = trips.get(names_key) or []
        for i, stop in enumerate(stops):
            slat = stop.get("Latitude") or stop.get("latitude")
            slon = stop.get("Longitude") or stop.get("longitude")
            if slat is None or slon is None:
                continue
            d = _haversine_m(lat, lon, slat, slon)
            name = names[i] if i < len(names) else stop.get("Name") or stop.get("name")
            if best is None or d < best[0]:
                best = (d, stop.get("Id") or stop.get("id"), name, dkey)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _eta_for_match(board, type_letter, route_number, target):
    for route in board.get("Routes", []):
        if route.get("Type") != type_letter:
            continue
        if str(route.get("Number", "")).strip() != str(route_number).strip():
            continue
        info = route.get("Info") or []
        for slot, idx in (("VehicleNumberFirst", 0), ("VehicleNumberSecond", 1)):
            num = (route.get(slot) or "").strip().lower()
            if num == target and idx < len(info):
                try:
                    return int(info[idx])
                except (ValueError, TypeError):
                    return None
    return None


def find_vehicle(client, vtype, gos_nomer, routes_cache):
    target = gos_nomer.strip().lower()
    type_letter = SCOREBOARD_TYPE_LETTER[vtype]
    log.info("find_vehicle: ищу '%s' [%s], маршрутов в кэше: %d",
             gos_nomer, vtype, len(routes_cache))

    if not routes_cache:
        log.warning("find_vehicle: кэш маршрутов пуст — поиск невозможен")
        return None

    for route_number, trips in routes_cache.items():
        try:
            data = client.get_vehicles(vtype, route_number)
        except Exception as e:
            log.debug("Vehicles[%s][%s] ошибка: %s", vtype, route_number, e)
            continue

        vehicles = data.get("Vehicles") or []
        # логируем первый маршрут с машинами для диагностики формата Id
        if vehicles and route_number == next(iter(routes_cache)):
            log.info("ДИАГНОСТИКА Vehicles[%s][%s]: первая машина=%s",
                     vtype, route_number, vehicles[0])

        for v in vehicles:
            vid = str(v.get("Id", "")).strip().lower()
            if vid != target:
                continue

            log.info("Нашёл машину '%s' на маршруте %s", gos_nomer, route_number)

            lat = v.get("Latitude") or v.get("latitude")
            lon = v.get("Longitude") or v.get("longitude")
            nearest = _nearest_stop(trips, lat, lon) if (lat and lon) else None
            stop_id, stop_name, dkey = nearest if nearest else (None, None, None)
            direction = trips.get(f"Name{dkey}") if dkey else None

            eta = None
            if stop_id is not None:
                try:
                    board = client.get_scoreboard(stop_id)
                    eta = _eta_for_match(board, type_letter, route_number, target)
                    if stop_name is None:
                        stop_name = board.get("StopName")
                except Exception as e:
                    log.warning("Scoreboard[%s] ошибка: %s", stop_id, e)

            return {
                "type": type_letter,
                "route": route_number,
                "direction": direction,
                "nearest_stop": stop_name,
                "eta_minutes": eta,
            }

    log.info("find_vehicle: машина '%s' не найдена ни на одном маршруте", gos_nomer)
    return None
