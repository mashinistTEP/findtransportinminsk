"""
minsktrans_client.py

Клиент для внутреннего (недокументированного) API "Виртуального табло"
Минсктранса (minsktrans.by/lookout_yard). Эндпоинты и формат ответов
получены реверс-инжинирингом трафика Chrome (PCAPdroid + TLS keylog),
проверено на живых данных 13.09.2026.

ПОДТВЕРЖДЕНО живым трафиком:
    POST /lookout_yard/Data/Scoreboard
        p=minsk&s={stop_id}&v={ts}&__RequestVerificationToken=...
        -> {"StopName": "...", "Routes": [
               {"Type": "Т"/"А"/"#", "Number": "27", "EndStop": "...",
                "Info": ["8", "18"],
                "VehicleNumberFirst": "5101", "VehicleNumberSecond": "5107"},
               ...]}
        Type: "А" — автобус, "Т" — троллейбус, "#" — трамвай (не буква).

    POST /lookout_yard/Data/Route
        p=minsk&tt={bus|trolleybus|tram}&r={route_number}&__RequestVerificationToken=...
        -> {"Trips": {"NameA": "X - Y", "StopsA": [{"Id":40101,"Latitude":..,"Longitude":..}],
                       "StopNamesA": ["X", ...], "NameB": "Y - X", "StopsB": [...], "StopNamesB": [...]}}

    POST /lookout_yard/Data/Vehicles  (это и есть основной поисковый запрос)
        p=minsk&tt={bus|trolleybus|tram}&r={route_number}&v={ts}&__RequestVerificationToken=...
        -> {"Vehicles": [{"Id": 5025, "IdEndStop": 65370, "TripType": 11,
                           "Latitude": 53.94775, "Longitude": 27.6414, "IsApparel": 1}, ...]}
        "Id" — гос. номер машины. Один запрос на МАРШРУТ отдаёт все машины
        на линии сразу, с живыми координатами — не нужно обходить остановки.

    tt=: "bus" и "tram" — просто слова целиком; по аналогии и trolleybus
    оказался целым словом "trolleybus" (проверено 14.09).

ПОКА НЕ РАЗОБРАНО:
    - POST /lookout_yard/Data/RouteList — по названию похоже на прямой список
      существующих маршрутов (заменил бы перебор номеров 1..150 в
      discover_routes() одним запросом), но тело ответа не успели поймать
      отдельно от шума в потоке. Не критично — discover_routes() и так работает.

Важно: этот код не проверялся на реальной сети (песочница без интернета),
писался строго по данным из расшифрованного трафика. Обкатывать и чинить
мелкие несостыковки (названия полей, регулярка токена и т.п.) — на живом
хостинге с доступом в интернет.
"""

import json
import os
import re
import time
import requests

BASE = "https://minsktrans.by/lookout_yard"
SLEEP_BETWEEN_REQUESTS = 0.2  # пауза между запросами — не долбить чужой сервер пачками

# tt= в запросах Data/Route, Data/Vehicles, Data/Track — все три подтверждены трафиком
VEHICLE_TYPES = {
    "bus": "bus",
    "trolley": "trolleybus",
    "tram": "tram",
}

# буква в поле "Type" ответа Data/Scoreboard
SCOREBOARD_TYPE_LETTER = {
    "bus": "А",      # подтверждено
    "trolley": "Т",  # подтверждено
    "tram": "#",     # подтверждено трафиком (14.09) — не буква, а решётка
}

ROUTES_CACHE_FILE = "routes_cache.json"


class MinsktransClient:
    def __init__(self, place="minsk"):
        self.place = place
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        })
        self.token = None
        self._bootstrap()

    def _bootstrap(self):
        """Открываем страницу табло, забираем куки сессии + anti-CSRF токен."""
        resp = self.session.get(f"{BASE}/Home/Index/{self.place}", timeout=15)
        resp.raise_for_status()
        m = re.search(
            r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', resp.text
        )
        if not m:
            raise RuntimeError(
                "Не нашёл __RequestVerificationToken на странице — "
                "разметка сайта могла измениться, нужно смотреть свежий HTML"
            )
        self.token = m.group(1)

    def _post(self, endpoint, data, retry=True):
        payload = dict(data)
        payload["__RequestVerificationToken"] = self.token
        resp = self.session.post(f"{BASE}/Data/{endpoint}", data=payload, timeout=15)
        if resp.status_code in (400, 403) and retry:
            # токен мог протухнуть — обновляем сессию один раз и пробуем снова
            self._bootstrap()
            return self._post(endpoint, data, retry=False)
        resp.raise_for_status()
        time.sleep(SLEEP_BETWEEN_REQUESTS)
        return resp.json()

    def get_scoreboard(self, stop_id):
        return self._post("Scoreboard", {
            "p": self.place, "s": stop_id, "v": int(time.time() * 1000),
        })

    def get_route(self, vtype, route_number):
        tt = VEHICLE_TYPES[vtype]
        return self._post("Route", {"p": self.place, "tt": tt, "r": route_number})

    def get_vehicles(self, vtype, route_number):
        tt = VEHICLE_TYPES[vtype]
        return self._post("Vehicles", {
            "p": self.place, "tt": tt, "r": route_number, "v": int(time.time() * 1000),
        })


def discover_routes(client, vtype, max_number=150):
    """
    Разовый обход: пробуем номера маршрутов 1..max_number для типа vtype,
    смотрим какие реально существуют (Data/Route отдаёт непустой Trips
    со списком остановок). Список маршрутов города меняется редко, поэтому
    результат стоит кэшировать на диск (см. load/save_routes_cache) и не
    гонять это на каждый поиск пользователя.
    """
    routes = {}
    for n in range(1, max_number + 1):
        try:
            data = client.get_route(vtype, n)
        except Exception:
            continue
        trips = data.get("Trips")
        if not trips or not (trips.get("StopsA") or trips.get("StopsB")):
            continue
        routes[str(n)] = trips
    return routes


def load_routes_cache(path=ROUTES_CACHE_FILE):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_routes_cache(cache, path=ROUTES_CACHE_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def get_or_build_routes(client, vtype, cache_path=ROUTES_CACHE_FILE):
    """Берёт маршруты типа vtype из кэша на диске, а если их там нет — строит и сохраняет."""
    cache = load_routes_cache(cache_path)
    if vtype not in cache:
        cache[vtype] = discover_routes(client, vtype)
        save_routes_cache(cache, cache_path)
    return cache[vtype]


def _haversine_m(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, asin, sqrt
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * 6371000 * asin(sqrt(a))


def _nearest_stop(trips, lat, lon):
    """По координатам машины находит ближайшую остановку в StopsA/StopsB
    этого маршрута (StopNamesA/StopNamesB — параллельные массивы имён,
    подтверждено трафиком 14.09). Возвращает (stop_id, stop_name, direction_key)
    где direction_key — 'A' или 'B'."""
    best = None
    for stops_key, names_key, dkey in (("StopsA", "StopNamesA", "A"), ("StopsB", "StopNamesB", "B")):
        stops = trips.get(stops_key) or []
        names = trips.get(names_key) or []
        for i, stop in enumerate(stops):
            d = _haversine_m(lat, lon, stop["Latitude"], stop["Longitude"])
            name = names[i] if i < len(names) else None
            if best is None or d < best[0]:
                best = (d, stop["Id"], name, dkey)
    if best is None:
        return None
    _, stop_id, name, dkey = best
    return stop_id, name, dkey


def _eta_for_match(board, type_letter, route_number, target):
    """Ищет в ответе Data/Scoreboard строку своего маршрута/типа с этим
    гос. номером и возвращает время до прибытия в минутах (или None)."""
    for route in board.get("Routes", []):
        if route.get("Type") != type_letter or str(route.get("Number")) != str(route_number):
            continue
        info = route.get("Info") or []
        for slot, idx in (("VehicleNumberFirst", 0), ("VehicleNumberSecond", 1)):
            num = (route.get(slot) or "").strip().lower()
            if num == target and idx < len(info):
                try:
                    return int(info[idx])
                except ValueError:
                    return None
    return None


def find_vehicle(client, vtype, gos_nomer, routes_cache):
    """
    Ищет машину gos_nomer среди маршрутов типа vtype: один запрос
    Data/Vehicles на каждый маршрут (а не на каждую остановку — так на
    порядок быстрее). Как только нашли совпадение по Id — по координатам
    машины находим ближайшую остановку её же маршрута, и одним точечным
    запросом Data/Scoreboard добираем время до прибытия.

    routes_cache: {"27": Trips-словарь, ...} — результат get_or_build_routes(),
    также используется для расчёта ближайшей остановки/направления.

    Возвращает dict {"type", "route", "direction", "nearest_stop", "eta_minutes"}
    либо None, если машина не найдена ни на одном маршруте этого типа.
    "type" — буква/символ как на самом табло (А/Т/#), показывать перед
    номером маршрута (например "Т" + "27" -> "Т27"). "eta_minutes" может
    быть None, если не удалось сопоставить со Scoreboard.
    """
    target = gos_nomer.strip().lower()
    type_letter = SCOREBOARD_TYPE_LETTER[vtype]

    for route_number, trips in routes_cache.items():
        try:
            data = client.get_vehicles(vtype, route_number)
        except Exception:
            continue
        for v in data.get("Vehicles", []):
            vid = str(v.get("Id", "")).strip().lower()
            if vid != target:
                continue

            nearest = _nearest_stop(trips, v["Latitude"], v["Longitude"])
            stop_id, stop_name, dkey = nearest if nearest else (None, None, None)
            direction = trips.get(f"Name{dkey}") if dkey else None

            eta = None
            if stop_id is not None:
                try:
                    board = client.get_scoreboard(stop_id)
                    eta = _eta_for_match(board, type_letter, route_number, target)
                    if stop_name is None:
                        stop_name = board.get("StopName")
                except Exception:
                    pass

            return {
                "type": type_letter,
                "route": route_number,
                "direction": direction,
                "nearest_stop": stop_name,
                "eta_minutes": eta,
            }
    return None


if __name__ == "__main__":
    # Пример использования — запускать только там, где есть интернет
    # (не в этой песочнице).
    client = MinsktransClient()
    routes = get_or_build_routes(client, "trolley")
    print(f"Найдено маршрутов: {len(routes)}")
    result = find_vehicle(client, "trolley", "5101", routes)
    print(result or "Маршрут не определён")
