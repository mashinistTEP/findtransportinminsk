"""
minsktrans_client.py — клиент lookout_yard API Минсктранса.
Эндпоинты восстановлены из реального трафика Chrome (PCAPdroid, 13-14.09.2026).

Ключевое замечание (обнаружено 16.09):
    Data/Vehicles.Id — это ВНУТРЕННИЙ номер депо, не гос. номер на табло.
    Гос. номер (то что показывает табло и что вводит пользователь) хранится
    в Data/Scoreboard -> Routes[i].VehicleNumberFirst / VehicleNumberSecond.
    Поэтому поиск строится так:
        1. Data/Vehicles(маршрут) — узнаём где находятся машины (координаты)
        2. Из координат ищем ближайшие остановки по данным маршрута
        3. Data/Scoreboard(остановка) — ищем гос. номер в VehicleNumberFirst/Second
"""

import json
import logging
import os
import re
import time
import requests

log = logging.getLogger("minsktrans")

BASE = "https://minsktrans.by/lookout_yard"
SLEEP_BETWEEN_REQUESTS = 0.5

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
        log.info("Bootstrap: получаю CSRF-токен…")
        resp = self.session.get(f"{BASE}/Home/Index/{self.place}", timeout=20)
        resp.raise_for_status()
        for p in [
            r'name="__RequestVerificationToken"[^>]*value="([^"]+)"',
            r'value="([^"]+)"[^>]*name="__RequestVerificationToken"',
            r'"__RequestVerificationToken"\s*:\s*"([^"]+)"',
        ]:
            m = re.search(p, resp.text)
            if m:
                self.token = m.group(1)
                log.info("Bootstrap OK (len=%d)", len(self.token))
                return
        log.error("Bootstrap FAIL. Начало страницы:\n%s", resp.text[:500])
        raise RuntimeError("Не нашёл __RequestVerificationToken")

    def _post(self, endpoint, data, retry=True):
        payload = dict(data)
        payload["__RequestVerificationToken"] = self.token
        try:
            resp = self.session.post(f"{BASE}/Data/{endpoint}", data=payload, timeout=20)
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
        if not resp.text.strip():
            # пустой ответ — скорее всего сессия протухла, обновляем токен
            if retry:
                log.warning("POST %s → пустой ответ (HTTP %d), обновляю токен", endpoint, resp.status_code)
                self._bootstrap()
                return self._post(endpoint, data, retry=False)
            log.error("POST %s → пустой ответ даже после обновления токена", endpoint)
            raise ValueError("empty response")
        try:
            return resp.json()
        except Exception:
            log.error("POST %s (HTTP %d): не JSON: %s", endpoint, resp.status_code, resp.text[:300])
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
    routes = {}
    log.info("discover_routes [%s]: проверяю 1..%d", vtype, max_number)
    try:
        sample = client.get_route(vtype, 1)
        trips_s = sample.get("Trips") or {}
        log.info("ДИАГНОСТИКА Route[%s][1] Trips keys: %s", vtype, list(trips_s.keys()))
    except Exception as e:
        log.warning("ДИАГНОСТИКА Route[%s][1] exception: %s", vtype, e)
    for n in range(1, max_number + 1):
        try:
            data = client.get_route(vtype, n)
        except Exception as e:
            log.debug("Route[%s][%d] ошибка: %s", vtype, n, e)
            continue
        trips = data.get("Trips") or data.get("trips")
        if not trips:
            continue
        stops_a = trips.get("StopsA") or []
        stops_b = trips.get("StopsB") or []
        if not stops_a and not stops_b:
            continue
        routes[str(n)] = trips
    log.info("discover_routes [%s]: итого %d маршрутов", vtype, len(routes))
    return routes


def load_routes_cache(path=ROUTES_CACHE_FILE):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Не удалось прочитать кэш: %s", e)
    return {}


def save_routes_cache(cache, path=ROUTES_CACHE_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        log.warning("Не удалось сохранить кэш: %s", e)


def get_or_build_routes(client, vtype, cache_path=ROUTES_CACHE_FILE):
    cache = load_routes_cache(cache_path)
    if vtype not in cache or not cache[vtype]:
        log.info("Кэш [%s] пуст — строю заново", vtype)
        cache[vtype] = discover_routes(client, vtype)
        save_routes_cache(cache, cache_path)
    else:
        log.info("Кэш [%s]: %d маршрутов (с диска)", vtype, len(cache[vtype]))
    return cache[vtype]


def _haversine_m(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, asin, sqrt
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1; dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return 2 * 6371000 * asin(sqrt(a))


def _nearest_stops(trips, lat, lon, n=3):
    """Возвращает n ближайших остановок маршрута к точке (lat, lon).
    Список: [(stop_id, stop_name, direction_key), ...]"""
    candidates = []
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
            name = names[i] if i < len(names) else stop.get("Name")
            sid = stop.get("Id") or stop.get("id")
            candidates.append((d, sid, name, dkey))
    candidates.sort(key=lambda x: x[0])
    return [(sid, name, dkey) for _, sid, name, dkey in candidates[:n]]


def find_vehicle(client, vtype, gos_nomer, routes_cache):
    """
    Алгоритм (исправлен 16.09 — Vehicles.Id ≠ гос. номер):
      1. Data/Vehicles(маршрут) → координаты всех машин на маршруте
      2. Для каждой машины → 3 ближайшие остановки из данных маршрута
      3. Data/Scoreboard(остановка) → ищем гос. номер в VehicleNumberFirst/Second
      4. Нашли → берём маршрут/направление/ETA прямо из Scoreboard
    """
    target = gos_nomer.strip().lower()
    type_letter = SCOREBOARD_TYPE_LETTER[vtype]
    log.info("find_vehicle: ищу '%s' [%s], маршрутов в кэше: %d",
             gos_nomer, vtype, len(routes_cache))

    if not routes_cache:
        log.warning("find_vehicle: кэш пуст")
        return None

    checked_stops = set()  # не проверяем одну остановку дважды

    for route_number, trips in routes_cache.items():
        try:
            data = client.get_vehicles(vtype, route_number)
        except Exception as e:
            log.debug("Vehicles[%s][%s]: %s", vtype, route_number, e)
            continue

        vehicles = data.get("Vehicles") or []
        if not vehicles:
            continue

        for v in vehicles:
            lat = v.get("Latitude") or v.get("latitude")
            lon = v.get("Longitude") or v.get("longitude")
            if not lat or not lon:
                continue

            for stop_id, stop_name, dkey in _nearest_stops(trips, lat, lon, n=1):
                if stop_id in checked_stops:
                    continue
                checked_stops.add(stop_id)

                try:
                    board = client.get_scoreboard(stop_id)
                except Exception as e:
                    log.debug("Scoreboard[%s]: %s", stop_id, e)
                    continue

                for route_entry in board.get("Routes") or []:
                    if route_entry.get("Type") != type_letter:
                        continue
                    info = route_entry.get("Info") or []
                    for slot, idx in (("VehicleNumberFirst", 0), ("VehicleNumberSecond", 1)):
                        num = (route_entry.get(slot) or "").strip().lower()
                        if num != target:
                            continue

                        # НАШЛИ
                        found_route = str(route_entry.get("Number", route_number))
                        log.info("Нашёл '%s' → маршрут %s, остановка '%s'",
                                 gos_nomer, found_route, board.get("StopName"))

                        try:
                            eta = int(info[idx])
                        except (ValueError, IndexError, TypeError):
                            eta = None

                        # направление: смотрим в кэше найденного маршрута
                        actual_trips = routes_cache.get(found_route, trips)
                        direction = None
                        for sk, dk2 in (("StopsA", "A"), ("StopsB", "B")):
                            if any(
                                str(s.get("Id", "")) == str(stop_id)
                                for s in (actual_trips.get(sk) or [])
                            ):
                                direction = actual_trips.get(f"Name{dk2}")
                                break

                        return {
                            "type": type_letter,
                            "route": found_route,
                            "direction": direction,
                            "nearest_stop": board.get("StopName") or stop_name,
                            "eta_minutes": eta,
                        }

    log.info("find_vehicle: '%s' не найден", gos_nomer)
    return None
