"""
bot.py — телеграм-бот поиска маршрута по гос. номеру ТС Минсктранса.

Работает через long polling (бот сам стучится к Telegram, входящих
подключений не принимает) — подходит для VPS без домена/HTTPS.

Запуск: BOT_TOKEN=... python3 bot.py
Токен бота берётся у @BotFather в Telegram.
"""

import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import telebot
from telebot import types

from minsktrans_client import (
    MinsktransClient,
    VEHICLE_TYPES,
    get_or_build_routes,
    find_vehicle,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN (переменная окружения)")

bot = telebot.TeleBot(BOT_TOKEN)
client = MinsktransClient()

TYPE_LABELS = {
    "bus": "🚌 Автобус",
    "trolley": "🚎 Троллейбус",
    "tram": "🚊 Трамвай",
}

# строим кэш маршрутов по всем типам один раз при старте — чтобы первый
# поиск пользователя не ждал ~30 сек на обход номеров 1..150
log.info("Строю кэш маршрутов (один раз, при следующих запусках возьмётся с диска)...")
ROUTES = {}
for vtype in VEHICLE_TYPES:
    ROUTES[vtype] = get_or_build_routes(client, vtype)
    log.info("  %s: %d маршрутов", vtype, len(ROUTES[vtype]))

user_state = {}  # chat_id -> {"step": "...", "vtype": "..."}


def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("Искать"))
    return kb


def type_menu():
    kb = types.InlineKeyboardMarkup()
    for vtype, label in TYPE_LABELS.items():
        kb.add(types.InlineKeyboardButton(label, callback_data=f"type:{vtype}"))
    return kb


def format_result(r):
    line = f"{r['type']}{r['route']}"
    if r.get("direction"):
        line += f"\n{r['direction']}"
    if r.get("nearest_stop"):
        eta = f", через {r['eta_minutes']} мин" if r.get("eta_minutes") is not None else ""
        line += f"\nБлижайшая остановка: {r['nearest_stop']}{eta}"
    return line


@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_state[message.chat.id] = {}
    bot.send_message(
        message.chat.id,
        "Привет! Это бот для поиска ПС по Минску. Укажи тип транспорта, гос. номер — найдется маршрут, ближайшая остановка и направление.\n\nВнимание! Бот может отвечать с задержкой.",
        reply_markup=main_menu(),
    )


@bot.message_handler(func=lambda m: m.text == "Искать")
def handle_search_button(message):
    user_state[message.chat.id] = {"step": "choose_type"}
    bot.send_message(message.chat.id, "Выбери тип транспорта:", reply_markup=type_menu())


@bot.callback_query_handler(func=lambda c: c.data.startswith("type:"))
def handle_type_choice(call):
    vtype = call.data.split(":", 1)[1]
    user_state[call.message.chat.id] = {"step": "await_number", "vtype": vtype}
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, f"Введи гос. номер ({TYPE_LABELS[vtype]}):")


@bot.message_handler(func=lambda m: user_state.get(m.chat.id, {}).get("step") == "await_number")
def handle_number(message):
    state = user_state.get(message.chat.id, {})
    vtype = state.get("vtype")
    gos_nomer = message.text.strip()

    bot.send_message(message.chat.id, "Ищу…")
    try:
        result = find_vehicle(client, vtype, gos_nomer, ROUTES[vtype])
    except Exception:
        log.exception("Ошибка поиска")
        bot.send_message(message.chat.id, "Не получилось выполнить поиск, попробуй ещё раз.", reply_markup=main_menu())
        user_state[message.chat.id] = {}
        return

    if result is None:
        bot.send_message(message.chat.id, "Маршрут не определён(возможно ПС не на маршруте)", reply_markup=main_menu())
    else:
        bot.send_message(message.chat.id, format_result(result), reply_markup=main_menu())
    user_state[message.chat.id] = {}


@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.send_message(message.chat.id, "Не понял. Нажми «Искать», чтобы начать поиск.", reply_markup=main_menu())


def _run_keepalive():
    """Крошечный HTTP-сервер — нужен только для того, чтобы Render не усыпил
    процесс. UptimeRobot пингует его каждые 5 минут на /health."""
    port = int(os.environ.get("PORT", 8080))

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass  # не засорять лог пингами

    HTTPServer(("0.0.0.0", port), _Handler).serve_forever()


if __name__ == "__main__":
    import time as _time
    threading.Thread(target=_run_keepalive, daemon=True).start()
    bot.remove_webhook()
    log.info("Бот запущен, жду сообщений")
    # При деплое на Render старый экземпляр может ещё жить несколько секунд.
    # Ловим 409 и повторяем попытку каждые 10 сек пока не освободится.
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                log.warning("409 Conflict — старый экземпляр ещё жив, жду 10 сек…")
                _time.sleep(10)
                continue
            raise
        except Exception as e:
            log.error("Ошибка polling: %s", e)
            _time.sleep(5)
