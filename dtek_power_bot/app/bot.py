"""Telegram-бот: графіки відключень ДТЕК за адресою або чергою, сповіщення про зміни."""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from dtek import REGIONS, Dtek, DtekError
from schedule import KYIV, group_label, has_changed, format_schedule, signature, today_kyiv

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# ---------- налаштування ----------
OPTIONS_FILE = Path("/data/options.json")
DATA_DIR = Path("/data") if Path("/data").is_dir() else Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

opts = json.loads(OPTIONS_FILE.read_text()) if OPTIONS_FILE.exists() else {}
TOKEN = opts.get("telegram_token") or os.environ.get("TELEGRAM_TOKEN", "")
POLL_MINUTES = int(opts.get("poll_minutes") or os.environ.get("POLL_MINUTES", 2))
def parse_people(raw) -> tuple[set[int], set[str]]:
    """Список зі змішаними числовими Telegram ID і @username -> (ID, імена в нижньому регістрі)."""
    items = [str(x).strip() for x in (raw or []) if str(x).strip()]
    ids = {int(x) for x in items if x.lstrip("-").isdigit()}
    names = {x.lstrip("@").lower() for x in items if not x.lstrip("-").isdigit()}
    return ids, names


ALLOWED_IDS, ALLOWED_NAMES = parse_people(opts.get("allowed_users") or os.environ.get("ALLOWED_USERS", "").split(","))
ADMIN_IDS, ADMIN_NAMES = parse_people(opts.get("admin_users") or os.environ.get("ADMIN_USERS", "").split(","))
# «Доступ для всіх»; якщо параметра немає — для всіх лише тоді, коли список порожній
ALLOW_ALL = bool(opts["allow_all"]) if "allow_all" in opts else not (ALLOWED_IDS or ALLOWED_NAMES)
DENY_MESSAGE = (opts.get("deny_message") or "").strip() or "Це приватний бот, доступ мають лише окремі люди."
STARTED_AT = time.time()

dtek = Dtek()


# ---------- підписки ----------
class Store:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict[str, dict] = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def get(self, chat_id) -> dict | None:
        return self.data.get(str(chat_id))

    def set(self, chat_id, sub: dict):
        self.data[str(chat_id)] = sub
        self.save()

    def delete(self, chat_id):
        self.data.pop(str(chat_id), None)
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
        tmp.replace(self.path)


store = Store(DATA_DIR / "subscriptions.json")


# ---------- статистика ----------
class Stats:
    """Лічильники для /stats: хто й коли користувався ботом, скільки сповіщень надіслано."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}
        for k in ("users", "sent", "denied"):
            self.data.setdefault(k, {})
        self.dirty = False
        # стан перевірок сайту за регіонами (лише в пам'яті)
        self.polls: dict[str, dict] = {}

    @staticmethod
    def today() -> str:
        return today_kyiv().isoformat()

    def touch(self, user_id: int):
        """Людина з доступом написала боту або натиснула кнопку."""
        d, key = self.today(), str(user_id)
        u = self.data["users"].get(key)
        if u is None:
            self.data["users"][key] = {"first": d, "last": d}
        elif u.get("last") != d:
            u["last"] = d
        else:
            return
        self.dirty = True
        self.save()

    def denied(self, user):
        self.data["denied"][str(user.id)] = {
            "name": user.full_name, "username": user.username,
            "at": datetime.now(KYIV).strftime("%d.%m %H:%M"),
        }
        # тримаємо лише останні 20 запитів доступу
        if len(self.data["denied"]) > 20:
            for k in list(self.data["denied"])[:-20]:
                del self.data["denied"][k]
        self.dirty = True
        self.save()

    def sent(self, n: int = 1):
        d = self.today()
        self.data["sent"][d] = self.data["sent"].get(d, 0) + n
        cutoff = (today_kyiv() - timedelta(days=60)).isoformat()
        for k in [k for k in self.data["sent"] if k < cutoff]:
            del self.data["sent"][k]
        self.dirty = True

    def save(self):
        if not self.dirty:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
        tmp.replace(self.path)
        self.dirty = False


stats = Stats(DATA_DIR / "stats.json")

_fact_cache: dict[str, tuple[float, dict]] = {}


async def get_fact(region: str, max_age: float = 60) -> dict:
    cached = _fact_cache.get(region)
    if cached and time.monotonic() - cached[0] < max_age:
        return cached[1]
    fact = await dtek.site(region).get_fact()
    _fact_cache[region] = (time.monotonic(), fact)
    return fact


# ---------- пошук адреси ----------
_PREFIX = re.compile(r"^(м|с|смт|сел|селище|с-ще|вул|вулиця|пров|провулок|просп|проспект|бульв|бульвар|пл|площа|шосе)\s+")
_LAT2CYR = str.maketrans({"a": "а", "e": "е", "o": "о", "c": "с", "i": "і", "p": "р", "x": "х", "k": "к"})


def norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("ʼ", "'").replace("`", "'").replace("ё", "е")
    s = re.sub(r"[.,\"«»()]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def bare(s: str) -> str:
    return _PREFIX.sub("", norm(s))


def search(query: str, options, limit: int = 10) -> list[str]:
    full = norm(query)
    q = bare(query) or full
    if not q:
        return []
    scored = []
    for o in options:
        n, b = norm(o), bare(o)
        if n == full:
            score = -1  # повний збіг разом із «м.», «с.», «вул.»
        elif b == q:
            score = 0
        elif b.startswith(q):
            score = 1
        elif all(t in n for t in q.split()):
            score = 2
        elif all(any(w.startswith(t[:max(3, len(t) - 2)]) for w in n.split()) for t in q.split()):
            score = 3  # інший порядок слів або відмінок: «Лесі Українки» / «Українки Лесі»
        else:
            continue
        scored.append((score, len(o), o))
    scored.sort()
    return [o for _, _, o in scored[:limit]]


def is_exact(found: list[str], query: str) -> bool:
    return len(found) == 1 or (bool(found) and norm(found[0]) == norm(query))


def norm_house(s: str) -> str:
    return re.sub(r"\s+", "", s.lower()).translate(_LAT2CYR)


# ---------- клавіатури ----------
def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


def chunks(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


REFRESH_KB = kb([[("🔄 Оновити", "now")]])

# ---------- тексти ----------
COMMANDS_TEXT = (
    "<b>Команди</b>\n"
    "/now — графік на сьогодні й завтра\n"
    "/start — вказати або змінити адресу чи чергу\n"
    "/stop — вимкнути сповіщення\n"
    "/help — що вміє бот"
)

WELCOME_TEXT = (
    "👋 <b>Вітаю!</b>\n\n"
    "Я стежу за графіками відключень на сайті ДТЕК і пишу, щойно ДТЕК змінює графік вашої черги "
    "або публікує графік на завтра.\n\n"
    f"{COMMANDS_TEXT}\n\n"
    "Щоб почати, оберіть регіон:"
)

HELP_TEXT = (
    "⚡️ <b>Що я вмію</b>\n"
    "• Пишу, щойно ДТЕК змінює графік вашої черги або публікує графік на завтра. "
    "Якщо змін немає — мовчу, опівночі не турбую.\n"
    "• У кожному повідомленні: графік на сьогодні, чи є світло зараз, коли наступна зміна, і графік на завтра.\n"
    "• Якщо ви вказали адресу — попереджаю про аварійні чи планові роботи на будинку, яких немає в графіку черг.\n"
    "• Якщо будинок у двох чергах — показую обидві.\n\n"
    f"{COMMANDS_TEXT}\n\n"
    "Під кожним графіком є кнопка «🔄 Оновити»."
)


# ---------- доступ ----------
def is_admin(user_id: int | None, username: str | None) -> bool:
    return (user_id in ADMIN_IDS) or bool(username and username.lower() in ADMIN_NAMES)


def is_allowed(user_id: int | None, username: str | None) -> bool:
    if ALLOW_ALL or is_admin(user_id, username):
        return True
    return (user_id in ALLOWED_IDS) or bool(username and username.lower() in ALLOWED_NAMES)


async def allowed(update: Update) -> bool:
    user = update.effective_user
    if user and is_allowed(user.id, user.username):
        stats.touch(user.id)
        # людина знову пише боту (наприклад, після зміни токена) — відновлюємо сповіщення
        sub = store.get(update.effective_chat.id) if update.effective_chat else None
        if sub and sub.get("paused"):
            sub["paused"] = False
            store.save()
            log.info("чат %s знову активний — сповіщення відновлено", update.effective_chat.id)
        return True
    log.info("Відмовлено в доступі: %s (@%s), ID %s",
             user.full_name if user else "?", user.username if user else "-", user.id if user else "?")
    if user:
        stats.denied(user)
    msg = (f"⛔ {html.escape(DENY_MESSAGE)}\n\n"
           f"Ваш Telegram ID: <code>{user.id if user else '?'}</code> — надішліть його власнику бота, "
           f"щоб він додав вас до списку.")
    if update.callback_query:
        await update.callback_query.answer("Немає доступу")
    if update.effective_chat:
        await update.effective_chat.send_message(msg, parse_mode=ParseMode.HTML)
    return False


# ---------- надсилання графіка ----------
async def current_outage_line(sub: dict) -> str:
    """Аварійні/планові роботи, які сайт показує для конкретного будинку (їх немає в графіку черг)."""
    if not sub.get("house"):
        return ""
    try:
        houses = await dtek.site(sub["region"]).get_houses(sub["city"], sub["street"])
    except DtekError:
        return ""
    h = houses.get(sub["house"]) or {}
    if not h.get("sub_type"):
        return ""
    line = f"⚠️ <b>За адресою зараз: {html.escape(h['sub_type'])}</b>"
    if h.get("start_date"):
        line += f"\nз {html.escape(h['start_date'])}"
    if h.get("end_date"):
        line += f", орієнтовно до {html.escape(h['end_date'])}"
    return line


async def send_schedule(bot, chat_id, sub: dict, header: str = "⚡️ <b>Графік відключень</b>",
                        fact: dict | None = None):
    if fact is None:
        fact = await get_fact(sub["region"])
    extra = await current_outage_line(sub)
    text = format_schedule(fact, sub["groups"], sub.get("label", ""), dtek.site(sub["region"]).names,
                           header=header, extra=extra)
    await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=REFRESH_KB)


async def save_and_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE, groups: list[str], **addr):
    region = ctx.user_data["region"]
    try:
        fact = await get_fact(region, max_age=0)
    except DtekError:
        await update.effective_chat.send_message("Не вдалося отримати графік із сайту ДТЕК. Спробуйте /now трохи згодом.")
        fact = {}
    user = update.effective_user
    sub = {"region": region, "groups": groups, **addr,
           "user_id": user.id if user else None, "username": user.username if user else None,
           "sig": signature(fact, groups) if fact else None, "update": fact.get("update")}
    store.set(update.effective_chat.id, sub)
    ctx.user_data.clear()
    await update.effective_chat.send_message(
        "✅ Готово. Я надішлю повідомлення, щойно ДТЕК змінить графік для вашої черги "
        "або опублікує графік на завтра.\n/now — графік зараз, /start — змінити адресу, /stop — відписатися."
    )
    if fact:
        await send_schedule(ctx.bot, update.effective_chat.id, sub, fact=fact)


# ---------- команди ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    ctx.user_data.clear()
    rows = [[(r["name"], f"r:{k}")] for k, r in REGIONS.items()]
    sub = store.get(update.effective_chat.id)
    if sub:
        what = sub.get("label") or ", ".join(group_label(g, dtek.site(sub["region"]).names) for g in sub["groups"])
        text = (f"Я стежу за: {what}.\nСповіщення увімкнено, графік зараз — /now.\n\n"
                "Щоб змінити адресу, оберіть регіон:")
    else:
        text = WELCOME_TEXT
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=kb(rows))


async def cmd_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    sub = store.get(update.effective_chat.id)
    if not sub:
        await update.effective_chat.send_message("Спершу вкажіть адресу або чергу: /start")
        return
    try:
        await send_schedule(ctx.bot, update.effective_chat.id, sub)
    except DtekError:
        await update.effective_chat.send_message("Сайт ДТЕК зараз не відповідає. Спробуйте за хвилину.")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    store.delete(update.effective_chat.id)
    await update.effective_chat.send_message("Сповіщення вимкнено. Щоб увімкнути знову — /start")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    await update.effective_chat.send_message(HELP_TEXT, parse_mode=ParseMode.HTML)


def _fmt_ago(ts: float) -> str:
    return datetime.fromtimestamp(ts, KYIV).strftime("%d.%m %H:%M")


def stats_text() -> str:
    today = today_kyiv()
    week_ago = (today - timedelta(days=6)).isoformat()
    t = today.isoformat()
    users = stats.data["users"].values()
    subs = list(store.data.values())

    paused = [s for s in subs if s.get("paused")]
    no_access = [s for s in subs if not s.get("paused") and not is_allowed(s.get("user_id"), s.get("username"))]
    active = len(subs) - len(paused) - len(no_access)
    by_region: dict[str, int] = {}
    for s in subs:
        by_region[s["region"]] = by_region.get(s["region"], 0) + 1
    by_addr = sum(1 for s in subs if s.get("house"))
    sent = stats.data["sent"]

    up = int(time.time() - STARTED_AT)
    lines = [
        "📊 <b>Статистика бота</b>",
        "",
        "👥 <b>Люди</b>",
        f"Запускали бота: {len(stats.data['users'])}",
        f"Нових: сьогодні {sum(u['first'] == t for u in users)}, за 7 днів {sum(u['first'] >= week_ago for u in users)}",
        f"Заходили в бота: сьогодні {sum(u['last'] == t for u in users)}, за 7 днів {sum(u['last'] >= week_ago for u in users)}",
        "",
        "🔔 <b>Підписки</b>",
        f"Усього: {len(subs)} (за адресою {by_addr}, за чергою {len(subs) - by_addr})",
        f"Отримують сповіщення: {active}",
    ]
    if paused:
        lines.append(f"На паузі (заблокували бота): {len(paused)}")
    if no_access:
        lines.append(f"Без доступу (прибрані зі списку): {len(no_access)}")
    for k, n in sorted(by_region.items(), key=lambda kv: -kv[1]):
        lines.append(f"• {REGIONS.get(k, {}).get('name', k)}: {n}")
    lines += [
        "",
        "✉️ <b>Сповіщень про зміни</b>",
        f"Сьогодні {sent.get(t, 0)}, за 7 днів {sum(v for k, v in sent.items() if k >= week_ago)}",
        "",
        "🌐 <b>Сайт ДТЕК</b>",
    ]
    if not stats.polls:
        lines.append("Ще не перевірявся (немає підписок або бот щойно запустився)")
    for k, p in sorted(stats.polls.items()):
        name = REGIONS.get(k, {}).get("name", k)
        if p.get("error"):
            since = f" з {_fmt_ago(p['error_at'])}" if p.get("error_at") else ""
            ok = f", востаннє вдало {_fmt_ago(p['ok'])}" if p.get("ok") else ""
            lines.append(f"⚠️ {name}: помилка{since}{ok}\n<code>{html.escape(p['error'][:150])}</code>")
        else:
            lines.append(f"✅ {name}: перевірено {_fmt_ago(p['ok'])}")
    lines.append(f"Перевірка кожні {POLL_MINUTES} хв · працює {up // 86400} д {up % 86400 // 3600} год {up % 3600 // 60} хв")

    denied = list(stats.data["denied"].items())[-5:]
    if denied and not ALLOW_ALL:
        lines += ["", "⛔ <b>Просили доступ</b> (додайте ID у «Дозволені користувачі»)"]
        for uid, d in reversed(denied):
            uname = f" @{d['username']}" if d.get("username") else ""
            lines.append(f"{html.escape(d.get('name') or '?')}{html.escape(uname)} — <code>{uid}</code>, {d.get('at', '')}")
    return "\n".join(lines)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id, user.username):
        if await allowed(update):
            await update.effective_chat.send_message("Ця команда доступна лише адміністраторам бота.")
        return
    stats.touch(user.id)
    await update.effective_chat.send_message(stats_text(), parse_mode=ParseMode.HTML)


# ---------- кнопки ----------
async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await allowed(update):
        return
    await q.answer()
    kind, _, val = q.data.partition(":")
    ud = ctx.user_data

    if kind == "now":
        await cmd_now(update, ctx)
        return

    if kind == "r":
        ud.clear()
        ud["region"] = val
        await q.edit_message_text(
            f"Регіон: {REGIONS[val]['name']}.\nЯк визначити вашу чергу?",
            reply_markup=kb([[("📍 За адресою", "m:addr")], [("🔢 Я знаю свою чергу", "m:grp")]]))
        return

    if "region" not in ud:
        await q.edit_message_text("Почнімо спочатку: /start")
        return
    site = dtek.site(ud["region"])

    if kind == "m" and val == "grp":
        try:
            await get_fact(ud["region"])
        except DtekError:
            await q.edit_message_text("Сайт ДТЕК не відповідає. Спробуйте /start трохи згодом.")
            return
        groups = sorted(site.names) or [f"GPV{a}.{b}" for a in range(1, 7) for b in (1, 2)]
        buttons = [(group_label(g, site.names).replace("Черга ", ""), f"g:{g}") for g in groups]
        await q.edit_message_text("Оберіть чергу:", reply_markup=kb(chunks(buttons, 4)))
        return

    if kind == "m" and val == "addr":
        ud["step"] = "city"
        await q.edit_message_text("Напишіть назву населеного пункту (наприклад: Бровари):")
        return

    if kind == "g":
        await q.edit_message_text(f"Обрано: {group_label(val, site.names)}")
        await save_and_show(update, ctx, [val])
        return

    cands = ud.get(f"cands_{kind}") or []
    try:
        choice = cands[int(val)]
    except (ValueError, IndexError):
        await q.edit_message_text("Кнопка застаріла. Почнімо спочатку: /start")
        return

    if kind == "c":
        await choose_city(update, ctx, choice, edit=True)
    elif kind == "s":
        await choose_street(update, ctx, choice, edit=True)
    elif kind == "h":
        await q.edit_message_text(f"Будинок: {choice}")
        await choose_house(update, ctx, choice)


async def ask(update: Update, text: str, markup=None, edit=False):
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_chat.send_message(text, reply_markup=markup)


async def offer(update: Update, ctx, prefix: str, items: list[str], title: str, per_row: int = 1):
    ctx.user_data[f"cands_{prefix}"] = items
    buttons = [(t, f"{prefix}:{i}") for i, t in enumerate(items)]
    await ask(update, title, kb(chunks(buttons, per_row)))


async def choose_city(update, ctx, city, edit=False):
    ctx.user_data.update(city=city, step="street")
    await ask(update, f"Населений пункт: {city}\nТепер напишіть вулицю:", edit=edit)


async def choose_street(update, ctx, street, edit=False):
    ctx.user_data.update(street=street, step="house")
    await ask(update, f"Вулиця: {street}\nНапишіть номер будинку:", edit=edit)


async def choose_house(update, ctx, house):
    ud = ctx.user_data
    houses = ud.get("houses") or {}
    groups = list((houses.get(house) or {}).get("sub_type_reason") or [])
    if not groups:
        ud["step"] = None
        await update.effective_chat.send_message(
            "Для цього будинку ДТЕК не вказує черги. Якщо знаєте її — оберіть вручну.",
            reply_markup=kb([[("🔢 Обрати чергу", "m:grp")]]))
        return
    label = f"{ud['city']}, {ud['street']}, {house}"
    await save_and_show(update, ctx, groups, label=label, city=ud["city"], street=ud["street"], house=house)


# ---------- текстові відповіді (адреса) ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    ud = ctx.user_data
    step = ud.get("step")
    text = (update.message.text or "").strip()
    if not step or "region" not in ud:
        await update.effective_chat.send_message("/now — графік зараз, /start — налаштувати адресу або чергу")
        return
    site = dtek.site(ud["region"])

    try:
        if step == "city":
            streets = await site.get_streets()
            found = search(text, streets.keys())
            if not found:
                await update.effective_chat.send_message("Не знайшов такого населеного пункту. Спробуйте інакше написати назву.")
            elif is_exact(found, text):
                await choose_city(update, ctx, found[0])
            else:
                await offer(update, ctx, "c", found, "Оберіть зі списку:", 2)

        elif step == "street":
            streets = (await site.get_streets()).get(ud["city"], [])
            found = search(text, streets, limit=12)
            if not found:
                await update.effective_chat.send_message("Не знайшов такої вулиці в цьому населеному пункті. Спробуйте частину назви.")
            elif is_exact(found, text):
                await choose_street(update, ctx, found[0])
            else:
                await offer(update, ctx, "s", found, "Оберіть вулицю:", 1)

        elif step == "house":
            houses = await site.get_houses(ud["city"], ud["street"])
            ud["houses"] = houses
            if not houses:
                await update.effective_chat.send_message(
                    "ДТЕК не повернув будинків для цієї вулиці.",
                    reply_markup=kb([[("🔢 Обрати чергу вручну", "m:grp")]]))
                return
            want = norm_house(text)
            exact = [h for h in houses if norm_house(h) == want]
            if exact:
                await choose_house(update, ctx, exact[0])
                return
            similar = [h for h in houses if norm_house(h).startswith(want)][:20]
            if not similar:
                similar = sorted(houses, key=lambda h: (len(h), h))[:20]
                title = "Такого номера немає. Ось будинки цієї вулиці:"
            else:
                title = "Оберіть будинок:"
            await offer(update, ctx, "h", similar, title, 4)
    except DtekError:
        await update.effective_chat.send_message("Сайт ДТЕК зараз не відповідає. Напишіть ще раз за хвилину.")


# ---------- перевірка змін ----------
async def poll(ctx: ContextTypes.DEFAULT_TYPE):
    subs = store.data
    for region in sorted({s["region"] for s in subs.values()}):
        try:
            fact = await get_fact(region, max_age=0)
        except DtekError as e:
            log.warning("[%s] не вдалося перевірити графік: %s", region, e)
            st = stats.polls.setdefault(region, {})
            if not st.get("error"):
                st["error_at"] = time.time()
            st["error"] = str(e)
            continue
        stats.polls[region] = {"ok": time.time()}
        for chat_id, sub in list(subs.items()):
            if sub["region"] != region:
                continue
            # доступ забрали в налаштуваннях — підписку тримаємо, але не пишемо
            if sub.get("paused") or not is_allowed(sub.get("user_id"), sub.get("username")):
                continue
            new = signature(fact, sub["groups"])
            old = sub.get("sig")
            # тимчасово порожня відповідь без нової дати оновлення — не вважаємо зміною
            if not any(new.values()) and old and any(old.values()) and fact.get("update") == sub.get("update"):
                continue
            if has_changed(old, new):
                log.info("[%s] графік змінився для чату %s", region, chat_id)
                try:
                    await send_schedule(ctx.bot, int(chat_id), sub, header="🔔 <b>Графік змінився</b>", fact=fact)
                    stats.sent()
                except Forbidden:
                    # бота заблокували або змінили токен на іншого бота, якому людина ще не писала.
                    # Адресу не видаляємо: щойно людина напише боту, сповіщення відновляться.
                    log.info("чат %s недоступний для бота — призупиняю сповіщення", chat_id)
                    sub["paused"] = True
                    continue
                except Exception as e:  # noqa: BLE001
                    log.warning("не вдалося надіслати в %s: %s", chat_id, e)
                    continue
            sub["sig"], sub["update"] = new, fact.get("update")
    store.save()
    stats.save()


async def post_init(app: Application):
    await dtek.start()
    await app.bot.set_my_commands([
        BotCommand("now", "Графік на сьогодні й завтра"),
        BotCommand("start", "Вказати або змінити адресу чи чергу"),
        BotCommand("stop", "Вимкнути сповіщення"),
        BotCommand("help", "Що вміє бот"),
    ])
    # адміністраторам (за числовим ID) — те саме меню плюс /stats
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.set_my_commands(
                [*(await app.bot.get_my_commands()), BotCommand("stats", "Статистика бота")],
                scope=BotCommandScopeChat(admin_id))
        except Exception as e:  # noqa: BLE001 — людина ще не писала боту
            log.info("меню /stats для %s поки не встановлено: %s", admin_id, e)
    # опис бота (порожній чат і профіль) задається вручну в @BotFather: /setdescription, /setabouttext
    app.job_queue.run_repeating(poll, interval=POLL_MINUTES * 60, first=15)
    log.info("Бот запущено. Перевірка кожні %d хв, підписок: %d", POLL_MINUTES, len(store.data))


async def post_shutdown(app: Application):
    await dtek.stop()


def main():
    if not TOKEN:
        raise SystemExit("Не вказано telegram_token у налаштуваннях add-on.")
    app = Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
