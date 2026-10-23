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
from schedule import (KYIV, fmt_when, format_schedule, group_label, has_changed, next_off_after, off_intervals,
                      signature, today_kyiv)

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
        for k in ("users", "sent", "denied", "polls"):
            self.data.setdefault(k, {})
        self.dirty = False
        if "since" not in self.data:  # коли почали вести статистику — видно, якщо дані колись зникнуть
            self.data["since"] = datetime.now(KYIV).strftime("%d.%m.%Y %H:%M")
            self.dirty = True
        # стан перевірок сайту за регіонами зберігається, щоб після перезапуску було видно останній результат
        self.polls: dict[str, dict] = self.data["polls"]

    def started(self, subs: dict):
        """Запуск бота: рахуємо перезапуски й додаємо людей з підписками, які користувались ботом
        ще до появи статистики (дата першого входу для них невідома)."""
        self.data["restarts"] = self.data.get("restarts", 0) + 1
        for sub in subs.values():
            uid = sub.get("user_id")
            if uid and str(uid) not in self.data["users"]:
                self.data["users"][str(uid)] = {"first": "", "last": "",
                                                "name": sub.get("name"), "username": sub.get("username")}
        self.dirty = True
        self.save()

    @staticmethod
    def today() -> str:
        return today_kyiv().isoformat()

    def touch(self, user):
        """Людина з доступом написала боту або натиснула кнопку."""
        d, key = self.today(), str(user.id)
        u = self.data["users"].get(key)
        if self.data["denied"].pop(key, None) is not None:  # людині щойно дали доступ
            self.dirty = True
        if u is None:
            self.data["users"][key] = {"first": d, "last": d, "name": user.full_name, "username": user.username}
        elif u.get("last") != d or u.get("name") != user.full_name or u.get("username") != user.username:
            u.update(last=d, name=user.full_name, username=user.username)
        elif not self.dirty:
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

    def sent(self, n: int = 1, kind: str = "sent"):
        """kind: sent — зміни графіка, remind — нагадування, emergency — аварійні роботи."""
        bucket = self.data.setdefault(kind, {})
        d = self.today()
        bucket[d] = bucket.get(d, 0) + n
        cutoff = (today_kyiv() - timedelta(days=60)).isoformat()
        for k in [k for k in bucket if k < cutoff]:
            del bucket[k]
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
_PREFIX = re.compile(r"^(м|с|смт|сел|селище|с ще|вул|вулиця|пров|провулок|просп|проспект|бульв|бульвар|пл|площа|шосе)\s+")
_LAT2CYR = str.maketrans({"a": "а", "e": "е", "o": "о", "c": "с", "i": "і", "p": "р", "x": "х", "k": "к"})


def norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("ʼ", "'").replace("`", "'").replace("ё", "е")
    # «№», «/», «-» теж роздільники: «Військове містечко №11» = «військове містечко 11», «в/ч-0565» = «в ч 0565»
    s = re.sub(r"[.,\"«»()№/\\_-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # скорочення, якими ДТЕК і люди пишуть військові об'єкти: «В/Ч 2860», «В/М 11», «вч 0565»
    s = re.sub(r"(?<!\w)(в ч|вч)(?!\w)", "військова частина", s)
    s = re.sub(r"(?<!\w)(в м|вм)(?!\w)", "військове містечко", s)
    return s


def compact(s: str) -> str:
    return re.sub(r"[\W_]+", "", norm(s))


_PREFIX_LAT = re.compile(r"^(m|s|smt|sel|selyshche|s shche|vul|vulytsia|prov|provulok|prosp|bulv|pl|shose|"
                         r"misto|selo|town|village|city|st|street|ave)\s+")


def bare(s: str) -> str:
    n = norm(s)
    return _PREFIX_LAT.sub("", _PREFIX.sub("", n)) if has_latin(n) else _PREFIX.sub("", n)


_NOT_PLACE = re.compile(r"^(\d{5}|україна|ukraine|.*област[ьі]|.*обл|.*oblast|.*район|.*raion|.*р н|.*громада|.*hromada)$")


def address_parts(text: str) -> list[str]:
    """«Vita-Poshtova, Kyiv Oblast, 08170» -> ["Vita-Poshtova"]: прибираємо область, район, країну, індекс."""
    parts = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
    keep = [p for p in parts if not _NOT_PLACE.match(norm(p))]
    return keep or parts


# Офіційна транслітерація (постанова КМУ №55, 2010) — так пишуть Google Maps і пошта: «Віта-Поштова» -> «vita-poshtova»
_TR = {"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh", "з": "z", "и": "y",
       "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s",
       "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "iu",
       "я": "ia", "'": ""}
_TR_START = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}


def translit(s: str) -> str:
    out, prev = [], " "
    for ch in s:
        out.append(_TR_START[ch] if ch in _TR_START and not prev.isalpha() else _TR.get(ch, ch))
        prev = ch
    # g/h і «зг» люди пишуть по-різному («Gostomel» / «Hostomel»)
    return "".join(out).replace("zgh", "zh").replace("g", "h")


def has_latin(s: str) -> bool:
    return bool(re.search(r"[a-z]", s.lower()))


def search(query: str, options, limit: int = 10, _latin: bool = False) -> list[str]:
    if has_latin(query) and not _latin:
        # назву написали латиницею — порівнюємо з транслітерованими варіантами ДТЕК
        q_lat = translit(norm(query))
        by_lat: dict[str, str] = {}
        for o in options:
            by_lat.setdefault(translit(norm(o)), o)
        found = [by_lat[x] for x in search(q_lat, list(by_lat), limit, _latin=True)]
        if found:
            return found
        # неофіційне написання: «Vishneve», «Hotiv», «Yasnogorodka»
        loose = lambda x: re.sub(r"(?<=[a-z])i([aeu])", r"\1", x.replace("kh", "h").replace("y", "i").replace("j", "i"))
        by_loose: dict[str, str] = {}
        for lat, o in by_lat.items():
            by_loose.setdefault(loose(lat), o)
        return [by_loose[x] for x in search(loose(q_lat), list(by_loose), limit, _latin=True)]
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
        elif all(any(w.startswith(t[:max(3, len(t) - 2)]) or (len(w) >= 3 and t.startswith(w))
                     for w in n.split()) for t in q.split()):
            # інший порядок слів, відмінок чи скорочення: «Лесі Українки» / «Українки Лесі», «військове» / «Військ.»
            score = 3
        elif len(compact(q)) >= 3 and compact(q) in compact(o):
            score = 4  # без пробілів і знаків: «вч0565» / «в/ч-0565»
        else:
            continue
        scored.append((score, len(o), o))
    scored.sort()
    return [o for _, _, o in scored[:limit]]


def is_exact(found: list[str], query: str) -> bool:
    """Обирати одразу, лише коли збіг однозначний. ДТЕК часто записує одне місце кількома способами
    («В/М 11», «Військове містечко №11», «Військове містечко-11») — тоді показуємо всі варіанти."""
    if len(found) == 1:
        return True
    q = norm(query)
    same = [f for f in found if norm(f) == q]
    # «Військове містечко» є, але поруч «Військове містечко-21» — краще спитати, ніж вгадати
    longer = [f for f in found if norm(f).startswith(q + " ")]
    return len(same) == 1 and found[0] == same[0] and not longer


def group_sort_key(g: str):
    return tuple(int(x) for x in re.findall(r"\d+", g)) or (10 ** 6,)


def house_key(h: str):
    """Природний порядок номерів: 2 < 10 < 10А < «в/ч-0565»."""
    m = re.match(r"\d+", h)
    return (int(m.group()) if m else 10 ** 9, h)


def norm_house(s: str) -> str:
    s = re.sub(r"\s+", "", s.lower()).translate(_LAT2CYR)
    s = s.rsplit(":", 1)[-1]  # службові префікси ДТЕК: «:0187», «К_Н_:5088»
    return re.sub(r"^0+(?=\d)", "", s)  # ДТЕК інколи дописує нулі попереду: «0254» = «254»


def no_street_options(streets) -> list[str]:
    """Записи на кшталт «Без вулиці», «Без назви» — так ДТЕК позначає села й місця без вулиць."""
    return [s for s in streets if re.search(r"без (вулиці|назви)", norm(s))]


def looks_like_house(text: str) -> bool:
    return bool(re.fullmatch(r"\d[\w/\\-]*", text.strip()))


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
    "/settings — нагадування й сповіщення\n"
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
    "• Нагадую перед відключенням (типово за 30 хв), а за бажанням — і коли за графіком мають дати світло.\n"
    "• Якщо ви вказали адресу — пишу, коли на будинку фактично вимкнули світло (буває на пів години раніше "
    "чи пізніше за графік), а також про аварійні чи планові роботи: початок, зміну часу й завершення.\n"
    "• Якщо будинок у двох чергах — показую обидві.\n"
    "• Що саме надсилати — у /settings.\n\n"
    f"{COMMANDS_TEXT}\n\n"
    "Під кожним графіком є кнопка «🔄 Оновити».\n\n"
    "<i>Вказана адреса зберігається на сервері власника бота і видна йому. "
    "Щоб її видалити — /stop.</i>"
)


# ---------- налаштування сповіщень ----------
DEFAULT_PREFS = {"before": 30, "restore": False, "emergency": True}
BEFORE_CHOICES = [(15, "15 хв"), (30, "30 хв"), (60, "1 год"), (0, "Не треба")]


def prefs(sub: dict) -> dict:
    return {**DEFAULT_PREFS, **(sub.get("prefs") or {})}


def settings_view(sub: dict) -> tuple[str, InlineKeyboardMarkup]:
    p = prefs(sub)
    before = next((t for m, t in BEFORE_CHOICES if m == p["before"]), f"{p['before']} хв")
    lines = ["⚙️ <b>Нагадування та сповіщення</b>", "",
             f"⏰ Нагадувати перед відключенням: <b>{'ні' if not p['before'] else 'за ' + before}</b>",
             f"💡 Писати, коли за графіком дають світло: <b>{'так' if p['restore'] else 'ні'}</b>"]
    rows = [[(("✓ " if p["before"] == m else "") + t, f"set:before:{m}") for m, t in BEFORE_CHOICES],
            [(("Не писати" if p["restore"] else "Писати") + ", коли дають світло", "set:restore")]]
    if sub.get("house"):
        lines.append(f"🔴 Писати, коли на будинку вимкнули світло, і про аварійні роботи: "
                     f"<b>{'так' if p['emergency'] else 'ні'}</b>")
        rows.append([(("Не писати" if p["emergency"] else "Писати") + " про відключення за адресою", "set:emergency")])
    else:
        lines.append("🔴 Відключення й аварійні роботи за адресою: "
                     "<i>доступно, якщо вказати адресу з номером будинку (/start)</i>")
    lines += ["", "<i>Зміни графіка я надсилаю завжди.</i>"]
    return "\n".join(lines), kb(rows)


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
        stats.touch(user)
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


def is_scheduled_outage(sub_type: str) -> bool:
    """ДТЕК позначає на будинку і звичайне відключення за графіком: «Стабілізаційне відключення (Згідно графіку
    погодинних відключень)». Тоді «Екстренні відключення (Аварійне без застосування графіку…)» — поза графіком."""
    t = (sub_type or "").lower()
    return "стабіліз" in t or ("згідно графіку" in t and "без застосування" not in t)


def _hm(dtek_time: str) -> str:
    """«14:33 23.10.2026» -> «14:33» (сьогодні) або як є."""
    parts = (dtek_time or "").split()
    if len(parts) == 2 and parts[1] == datetime.now(KYIV).strftime("%d.%m.%Y"):
        return parts[0]
    return dtek_time


def scheduled_until(sub: dict, fact: dict | None, now: datetime | None = None) -> datetime | None:
    """Кінець відключення за поточним графіком, до якого належить фактичне відключення зараз.
    Реальний час розходиться з графіком на ±30 хв, тому шукаємо з запасом 45 хв."""
    if not fact:
        return None
    now = now or datetime.now(KYIV)
    best = None
    for g in sub["groups"]:
        for a, b in off_intervals(fact, g, now):
            if b and a - timedelta(minutes=45) <= now < b + timedelta(minutes=45):
                if best is None or abs((a - now).total_seconds()) < abs((best[0] - now).total_seconds()):
                    best = (a, b)
    return best[1] if best else None


def emergency_state(h: dict | None) -> dict | None:
    """Аварійні/планові роботи на будинку з відповіді getHomeNum, або None, якщо їх немає."""
    if not h or not h.get("sub_type"):
        return None
    return {"type": h["sub_type"], "start": h.get("start_date") or "", "end": h.get("end_date") or ""}


# ---------- надсилання графіка ----------
async def current_outage_line(sub: dict, fact: dict | None = None) -> str:
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
    if is_scheduled_outage(h["sub_type"]):
        until = scheduled_until(sub, fact)
        line = "🔴 <b>За адресою зараз відключення</b>"
        if h.get("start_date"):
            line += f" — світло вимкнули о {html.escape(_hm(h['start_date']))}"
        if until:
            line += f"\nЗа графіком до {until.strftime('%H:%M')}"
        elif h.get("end_date"):
            line += f"\nОрієнтовно до {html.escape(_hm(h['end_date']))}"
        return line
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
    extra = await current_outage_line(sub, fact)
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
    old = store.get(update.effective_chat.id) or {}
    if addr.get("house"):
        h = (ctx.user_data.get("houses") or {}).get(addr["house"])
        addr.update(emergency=emergency_state(h), em_checked=True)
    sub = {"region": region, "groups": groups, **addr, "prefs": old.get("prefs") or {},
           "user_id": user.id if user else None, "username": user.username if user else None,
           "name": user.full_name if user else None,
           "sig": signature(fact, groups) if fact else None, "update": fact.get("update")}
    store.set(update.effective_chat.id, sub)
    ctx.user_data.clear()
    p = prefs(sub)
    await update.effective_chat.send_message(
        "✅ Готово. Я надішлю повідомлення, щойно ДТЕК змінить графік для вашої черги "
        "або опублікує графік на завтра."
        + (f" Перед кожним відключенням нагадаю за {p['before']} хв." if p["before"] else "")
        + (" Також напишу, коли на будинку вимкнуть світло, і про аварійні роботи." if sub.get("house") and p["emergency"] else "")
        + "\n/now — графік зараз, /settings — нагадування, /start — змінити адресу, /stop — відписатися."
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


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await allowed(update):
        return
    sub = store.get(update.effective_chat.id)
    if not sub:
        await update.effective_chat.send_message("Спершу вкажіть адресу або чергу: /start")
        return
    text, markup = settings_view(sub)
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML, reply_markup=markup)


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
        f"Запускали бота: {len(stats.data['users'])}"
        + (f" · без доступу писали: {len(stats.data['denied'])}" if stats.data["denied"] else ""),
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
        "✉️ <b>Надіслано</b> (сьогодні / за 7 днів)",
        *[f"{title}: {b.get(t, 0)} / {sum(v for k, v in b.items() if k >= week_ago)}"
          for title, b in (("Зміни графіка", sent), ("Нагадування", stats.data.get("remind", {})),
                           ("Аварійні роботи", stats.data.get("emergency", {})))],
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
    lines += [
        "",
        "⚙️ <b>Бот</b>",
        f"Запущено {_fmt_ago(STARTED_AT)} · працює без перезапуску {up // 86400} д {up % 86400 // 3600} год {up % 3600 // 60} хв",
        f"Перезапусків: {stats.data.get('restarts', 0)} · статистика ведеться з {stats.data['since']}",
        f"Перевірка сайту кожні {POLL_MINUTES} хв",
    ]

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
    stats.touch(user)
    await update.effective_chat.send_message(stats_text(), parse_mode=ParseMode.HTML,
                                             reply_markup=kb([[("👥 Люди та їхні адреси", "adm:users")]]))


def users_text() -> list[str]:
    """Усі люди зі статистики й підписок: хто, за чим стежить, коли заходив. Ділиться на кілька повідомлень."""
    people: dict[str, dict] = {k: dict(v) for k, v in stats.data["users"].items()}
    for chat_id, sub in store.data.items():
        key = str(sub.get("user_id") or chat_id)
        p = people.setdefault(key, {})
        p.setdefault("name", sub.get("name"))
        p.setdefault("username", sub.get("username"))
        p.setdefault("subs", []).append((chat_id, sub))

    def fmt_date(d):
        return f"{d[8:10]}.{d[5:7]}" if d else "—"

    # спершу ті, хто заходив нещодавно
    order = sorted(people.items(), key=lambda kv: (kv[1].get("last") or "", kv[1].get("first") or ""), reverse=True)
    blocks = []
    for i, (uid, p) in enumerate(order, 1):
        who = html.escape(p.get("name") or "Без імені")
        if p.get("username"):
            who += f" @{html.escape(p['username'])}"
        lines = [f"<b>{i}. {who}</b> · <code>{uid}</code>"]
        subs = p.get("subs") or []
        for chat_id, sub in subs:
            names = dtek.site(sub["region"]).names if sub["region"] in dtek.sites else None
            groups = ", ".join(group_label(g, names) for g in sub.get("groups", []))
            what = f"{html.escape(sub['label'])} · {groups}" if sub.get("label") else groups
            if sub.get("paused"):
                state = "⏸ на паузі"
            elif not is_allowed(sub.get("user_id"), sub.get("username")):
                state = "⛔ без доступу"
            else:
                state = "🔔"
            where = REGIONS.get(sub["region"], {}).get("name", sub["region"])
            group_chat = " (груповий чат)" if str(chat_id) != uid else ""
            lines.append(f"{state} 📍 {what}\n      {where}{group_chat}")
        if not subs:
            lines.append("— адресу чи чергу ще не вказав")
        lines.append(f"<i>перший вхід {fmt_date(p.get('first'))} · останній {fmt_date(p.get('last'))}</i>")
        blocks.append("\n".join(lines))
    if not blocks:
        return ["Поки що ніхто не користувався ботом."]

    # Telegram приймає до 4096 символів в одному повідомленні
    header = f"👥 <b>Люди та їхні адреси</b> ({len(blocks)})\n\n"
    pages, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > 3800:
            pages.append(cur.rstrip())
            cur = ""
        cur += b + "\n\n"
    pages.append(cur.rstrip())
    return pages


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id, user.username):
        if await allowed(update):
            await update.effective_chat.send_message("Ця команда доступна лише адміністраторам бота.")
        return
    stats.touch(user)
    for page in users_text():
        await update.effective_chat.send_message(page, parse_mode=ParseMode.HTML)


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

    if kind == "adm" and val == "users":
        await cmd_users(update, ctx)
        return

    if kind == "set":
        sub = store.get(update.effective_chat.id)
        if not sub:
            await q.edit_message_text("Спершу вкажіть адресу або чергу: /start")
            return
        p = sub.setdefault("prefs", {})
        what, _, arg = val.partition(":")
        if what == "before":
            p["before"] = int(arg)
        elif what in ("restore", "emergency"):
            p[what] = not prefs(sub)[what]
        store.save()
        text, markup = settings_view(sub)
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception:  # noqa: BLE001 — «message is not modified», якщо натиснули вже вибране
            pass
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
        fact = _fact_cache.get(ud["region"], (0, {}))[1]
        in_fact = {g for day in (fact.get("data") or {}).values() if isinstance(day, dict) for g in day} \
            if isinstance(fact.get("data"), dict) else set()
        # у Києві 60 черг — показуємо ті, що є в графіку, у природному порядку (1.1, 1.2, 2.1 … 10.1)
        groups = sorted(in_fact or site.names or {f"GPV{a}.{b}" for a in range(1, 7) for b in (1, 2)}, key=group_sort_key)[:100]
        buttons = [(group_label(g, site.names).replace("Черга ", ""), f"g:{g}") for g in groups]
        await q.edit_message_text("Оберіть чергу:", reply_markup=kb(chunks(buttons, 5 if len(buttons) > 24 else 4)))
        return

    if kind == "m" and val == "addr":
        try:
            streets = await site.get_streets()
        except DtekError:
            await q.edit_message_text("Сайт ДТЕК зараз не відповідає. Спробуйте /start трохи згодом.")
            return
        if len(streets) == 1:
            # Київ: окремого кроку «населений пункт» на сайті немає — одразу питаємо вулицю
            await choose_city(update, ctx, next(iter(streets)), edit=True)
            return
        ud["step"] = "city"
        example = REGIONS[ud["region"]].get("example")
        await q.edit_message_text("Напишіть назву населеного пункту" + (f" (наприклад: {example}):" if example else ":"))
        return

    if kind == "g":
        await q.edit_message_text(f"Обрано: {group_label(val, site.names)}")
        if ud.get("city") and ud.get("street"):
            # черга для конкретного місця, де ДТЕК не дав однозначної відповіді
            parts = [ud["city"], ud["street"]] + ([ud["picked_house"]] if ud.get("picked_house") else [])
            await save_and_show(update, ctx, [val], label=", ".join(parts), city=ud["city"], street=ud["street"])
        else:
            await save_and_show(update, ctx, [val])
        return

    if kind == "hall":
        houses = ud.get("houses") or {}
        cands = ud.get("cands_h") or []
        groups = sorted({g for h in cands for g in (houses.get(h) or {}).get("sub_type_reason") or []})
        if not groups:
            await q.edit_message_text("Кнопка застаріла. Почнімо спочатку: /start")
            return
        typed = ud.get("house_typed") or cands[0]
        await q.edit_message_text(f"Будинок: {typed} — стежу за всіма записами ДТЕК з цим номером.")
        await save_and_show(update, ctx, groups, label=f"{ud['city']}, {ud['street']}, {typed}",
                            city=ud["city"], street=ud["street"], house=cands[0])
        return

    if kind == "nh":
        houses = ud.get("houses") or {}
        sets = {tuple(sorted(v.get("sub_type_reason") or [])) for v in houses.values()} - {()}
        label = f"{ud.get('city')}, {ud.get('street')}"
        if len(sets) == 1:
            await q.edit_message_text(f"Уся адреса «{label}» в одній черзі — номер не потрібен.")
            await save_and_show(update, ctx, list(next(iter(sets))), label=label,
                                city=ud.get("city"), street=ud.get("street"))
        elif sets:
            groups = sorted({g for gs in sets for g in gs})
            buttons = [(group_label(g, site.names), f"g:{g}") for g in groups]
            await q.edit_message_text(
                f"Будинки за адресою «{label}» у різних чергах. Напишіть номер будинку "
                "або оберіть чергу, якщо знаєте її (можна спитати в сусідів):",
                reply_markup=kb(chunks(buttons, 3)))
        else:
            await q.edit_message_text("ДТЕК не вказує черги для цієї адреси. Якщо знаєте свою — оберіть:",
                                      reply_markup=kb([[("🔢 Обрати чергу", "m:grp")]]))
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
    ud = ctx.user_data
    ud.update(city=city, step="street")
    try:
        streets = (await dtek.site(ud["region"]).get_streets()).get(city, [])
    except DtekError:
        streets = []
    nostreet = no_street_options(streets)
    text = f"Населений пункт: {city}\nТепер напишіть вулицю:"
    markup = None
    if nostreet:
        ud["cands_s"] = nostreet
        markup = kb([[(f"🏡 {s}", f"s:{i}")] for i, s in enumerate(nostreet)])
        text += "\n\nЯкщо вулиці немає — натисніть кнопку нижче або просто напишіть номер будинку."
    await ask(update, text, markup, edit=edit)


async def choose_street(update, ctx, street, edit=False, house_hint: str | None = None):
    ud = ctx.user_data
    ud.update(street=street, step="house")
    try:
        houses = await dtek.site(ud["region"]).get_houses(ud["city"], street)
    except DtekError:
        ud["step"] = "street"
        await ask(update, "Сайт ДТЕК зараз не відповідає. Напишіть вулицю ще раз за хвилину.", edit=edit)
        return
    ud["houses"] = houses
    if not houses:
        ud["step"] = None
        await ask(update, f"Вулиця: {street}\nДТЕК не має будинків за цією адресою. Якщо знаєте чергу — оберіть вручну, "
                  "або спробуйте інший варіант назви: /start",
                  kb([[("🔢 Обрати чергу", "m:grp")]]), edit=edit)
        return
    if len(houses) == 1:
        # військова частина чи окремий об'єкт — запис лише один, номер питати немає сенсу
        only = next(iter(houses))
        await ask(update, f"Адреса: {street}\nЗа нею в ДТЕК лише один запис: {only}", edit=edit)
        await choose_house(update, ctx, only)
        return
    if house_hint:
        # номер уже написали (наприклад, одразу замість вулиці) — шукаємо його без зайвих питань
        await ask(update, f"Адреса: {street}", edit=edit)
        await pick_house(update, ctx, house_hint)
        return
    items = sorted(houses, key=house_key)
    rows = []
    if len(items) <= 24:
        ud["cands_h"] = items
        rows = chunks([(h, f"h:{i}") for i, h in enumerate(items)], 4)
        title = f"Вулиця: {street}\nОберіть будинок або напишіть номер:"
    else:
        title = f"Вулиця: {street}\nНапишіть номер будинку:"
    rows.append([("🤷 Номера немає / не знаю", "nh:")])
    await ask(update, title, kb(rows), edit=edit)


async def choose_house(update, ctx, house):
    ud = ctx.user_data
    houses = ud.get("houses") or {}
    groups = list((houses.get(house) or {}).get("sub_type_reason") or [])
    if not groups:
        ud["step"] = None
        ud["picked_house"] = house
        await update.effective_chat.send_message(
            "Для цього будинку ДТЕК не вказує черги. Якщо знаєте її — оберіть вручну.",
            reply_markup=kb([[("🔢 Обрати чергу", "m:grp")]]))
        return
    label = f"{ud['city']}, {ud['street']}, {house}"
    await save_and_show(update, ctx, groups, label=label, city=ud["city"], street=ud["street"], house=house)


async def pick_house(update, ctx, text: str):
    """Знайти будинок за введеним номером: точний збіг, потім схожі, потім найближчі за номером."""
    ud = ctx.user_data
    houses = ud.get("houses") or {}
    if not houses:
        await update.effective_chat.send_message(
            "ДТЕК не повернув будинків для цієї вулиці.",
            reply_markup=kb([[("🔢 Обрати чергу вручну", "m:grp")]]))
        return
    want = norm_house(text)
    exact = [h for h in houses if norm_house(h) == want]
    groupsets = {tuple(sorted(houses[h].get("sub_type_reason") or [])) for h in exact}
    if len(exact) == 1 or (exact and len(groupsets) == 1):
        await choose_house(update, ctx, exact[0])
        return
    if exact:
        # ДТЕК має кілька записів з цим номером у різних чергах («5086» і «:5086») — вгадувати не будемо
        names = dtek.site(ud["region"]).names
        exact = sorted(exact, key=house_key)
        ud["cands_h"] = exact
        ud["house_typed"] = text.strip()
        rows = [[(f"{h} · " + ", ".join(group_label(g, names) for g in houses[h].get("sub_type_reason") or []),
                  f"h:{i}")] for i, h in enumerate(exact)]
        rows.append([("✅ Стежити за всіма", "hall:")])
        await ask(update, "У ДТЕК кілька записів із цим номером, і вони в різних чергах. "
                          "Оберіть свій або стежте за всіма — тоді я писатиму про зміни в кожній:", kb(rows))
        return
    else:
        similar = sorted((h for h in houses if norm_house(h).startswith(want)), key=house_key)
        if not similar:
            similar = sorted((h for h in houses if want and want in norm_house(h)), key=house_key)
        title = "Оберіть будинок:"
        if not similar:
            num = house_key(want)[0]
            # найближчі за номером, щоб не показувати випадкові 20 будинків зі 150
            nearest = sorted(houses, key=lambda h: (abs(house_key(norm_house(h))[0] - num), house_key(h)))[:12]
            similar = sorted(nearest, key=lambda h: house_key(norm_house(h)))
            title = "Такого номера немає. Найближчі за номером:"
    similar = similar[:20]
    ud["cands_h"] = similar
    rows = chunks([(h, f"h:{i}") for i, h in enumerate(similar)], 4)
    rows.append([("🤷 Номера немає / не знаю", "nh:")])
    await ask(update, title, kb(rows))


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
                # адреса, скопійована з карти чи листа: «Vita-Poshtova, Kyiv Oblast, 08170»
                for part in address_parts(text):
                    found = search(part, streets.keys())
                    if found:
                        text = part
                        break
            if not found:
                await update.effective_chat.send_message("Не знайшов такого населеного пункту. Спробуйте інакше написати назву.")
            elif is_exact(found, text):
                await choose_city(update, ctx, found[0])
            else:
                await offer(update, ctx, "c", found, "Оберіть зі списку:", 2)

        elif step == "street":
            streets = (await site.get_streets()).get(ud["city"], [])
            found = search(text, streets, limit=12)
            nostreet = no_street_options(streets)
            if not found and nostreet and looks_like_house(text):
                # написали лише номер: шукаємо його серед «Без вулиці» / «Без назви»
                target = nostreet[0]
                for opt in nostreet:
                    if any(norm_house(h) == norm_house(text) for h in await site.get_houses(ud["city"], opt)):
                        target = opt
                        break
                await choose_street(update, ctx, target, house_hint=text)
            elif not found:
                await update.effective_chat.send_message(
                    "Не знайшов такої вулиці в цьому населеному пункті. Спробуйте частину назви"
                    + (" або напишіть лише номер будинку, якщо вулиці немає." if nostreet else "."))
            elif is_exact(found, text):
                await choose_street(update, ctx, found[0])
            else:
                await offer(update, ctx, "s", found, "Оберіть вулицю:", 1)

        elif step == "house":
            if ud.get("houses") is None:
                ud["houses"] = await site.get_houses(ud["city"], ud["street"])
            await pick_house(update, ctx, text)
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
            stats.dirty = True
            continue
        stats.polls[region] = {"ok": time.time()}
        stats.dirty = True
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
        await check_emergency(ctx, region)
    store.save()
    stats.save()


async def _send(ctx, chat_id, sub: dict, text: str, kind: str) -> bool:
    try:
        await ctx.bot.send_message(int(chat_id), text, parse_mode=ParseMode.HTML, reply_markup=REFRESH_KB)
        stats.sent(kind=kind)
        return True
    except Forbidden:
        log.info("чат %s недоступний для бота — призупиняю сповіщення", chat_id)
        sub["paused"] = True
    except Exception as e:  # noqa: BLE001
        log.warning("не вдалося надіслати в %s: %s", chat_id, e)
    return False


def _place(sub: dict, groups: list[str]) -> str:
    names = dtek.site(sub["region"]).names
    g = html.escape(", ".join(group_label(x, names) for x in groups))
    return f"📍 {html.escape(sub['label'])} · {g}" if sub.get("label") else f"🔢 {g}"


async def remind(ctx: ContextTypes.DEFAULT_TYPE):
    """Щохвилини: нагадування перед відключенням і в момент, коли за графіком мають дати світло."""
    now = datetime.now(KYIV)
    changed = False
    for chat_id, sub in list(store.data.items()):
        if sub.get("paused") or not is_allowed(sub.get("user_id"), sub.get("username")):
            continue
        cached = _fact_cache.get(sub["region"])
        if not cached:
            continue
        fact, p = cached[1], prefs(sub)
        sent = sub.setdefault("reminded", [])
        # події з кількох черг одного будинку об'єднуємо, щоб не дублювати
        before: dict[datetime, tuple[list[str], datetime | None]] = {}
        restore: dict[datetime, list[str]] = {}
        for g in sub["groups"]:
            for start, end in off_intervals(fact, g, now):
                if p["before"] and start - timedelta(minutes=p["before"]) <= now < start:
                    gs, e = before.setdefault(start, ([], end))
                    gs.append(g)
                if p["restore"] and end and end <= now < end + timedelta(minutes=10):
                    restore.setdefault(end, []).append(g)
        for start, (gs, end) in sorted(before.items()):
            key = f"b|{start.isoformat()}"
            if key in sent:
                continue
            mins = max(1, round((start - now).total_seconds() / 60))
            text = (f"⏰ <b>Через {mins} хв відключення</b> — {fmt_when(start, now)}\n{_place(sub, gs)}\n"
                    f"🔴 {start.strftime('%H:%M')}–{end.strftime('%H:%M') if end else '…'}"
                    + ("" if end else " (кінець ще невідомий — графіка на наступну добу немає)")
                    + "\n<i>Встигніть зарядити телефон і павербанк.</i>")
            if await _send(ctx, chat_id, sub, text, "remind"):
                log.info("нагадування перед відключенням %s для чату %s", start, chat_id)
            sent.append(key)
            changed = True
        for end, gs in sorted(restore.items()):
            key = f"r|{end.isoformat()}"
            if key in sent:
                continue
            nxt = next_off_after(fact, sub["groups"], end)
            text = (f"💡 <b>За графіком світло вже має бути</b> (з {end.strftime('%H:%M')})\n{_place(sub, gs)}\n"
                    + (f"Наступне відключення {fmt_when(nxt, now)}." if nxt else "Інших відключень у графіку поки немає."))
            await _send(ctx, chat_id, sub, text, "remind")
            sent.append(key)
            changed = True
        if len(sent) > 60:
            del sent[:-60]
    if changed:
        store.save()
        stats.save()


async def check_emergency(ctx: ContextTypes.DEFAULT_TYPE, region: str):
    """Аварійні й планові роботи на будинках підписників: початок, зміна часу відновлення, завершення."""
    subs = [(cid, s) for cid, s in store.data.items()
            if s["region"] == region and s.get("house") and prefs(s)["emergency"]
            and not s.get("paused") and is_allowed(s.get("user_id"), s.get("username"))]
    houses_by_street: dict[tuple[str, str], dict] = {}
    for chat_id, sub in subs:
        key = (sub.get("city") or "", sub["street"])
        if key not in houses_by_street:
            try:
                houses_by_street[key] = await dtek.site(region).get_houses(*key)
            except DtekError as e:
                log.warning("[%s] не вдалося перевірити аварійні роботи для %s: %s", region, key, e)
                houses_by_street[key] = None
        houses = houses_by_street[key]
        if not houses or sub["house"] not in houses:
            continue  # немає даних — нічого не вирішуємо
        new, old = emergency_state(houses[sub["house"]]), sub.get("emergency")
        if not sub.get("em_checked"):
            sub.update(emergency=new, em_checked=True)  # перша перевірка: лише запам'ятати стан
            continue
        text = None
        place = f"📍 {html.escape(sub.get('label', ''))}"
        new_sched = bool(new) and is_scheduled_outage(new["type"])
        old_sched = bool(old) and is_scheduled_outage(old["type"])
        if new_sched:
            # звичайне відключення за графіком: повідомляємо лише момент, коли світло фактично вимкнули
            if not old_sched or old["start"] != new["start"]:
                until = scheduled_until(sub, _fact_cache.get(region, (0, None))[1])
                text = ("🔴 <b>Відключення</b>"
                        + (f" — світло вимкнули о {html.escape(_hm(new['start']))}" if new["start"] else "")
                        + f"\n{_place(sub, sub['groups'])}"
                        + (f"\nЗа графіком до {until.strftime('%H:%M')}" if until
                           else f"\nОрієнтовно до {html.escape(_hm(new['end']))}" if new["end"] else ""))
        elif old_sched and not new:
            pass  # відключення за графіком закінчилось — окремо не пишемо
        elif new and (not old or old["type"] != new["type"]):
            text = (f"⚠️ <b>Відключення за вашою адресою</b>\n{place}\n{html.escape(new['type'])}"
                    + (f"\nПочаток: {html.escape(new['start'])}" if new["start"] else "")
                    + (f"\nОрієнтовно до: <b>{html.escape(new['end'])}</b>" if new["end"] else "")
                    + "\n<i>Це окремо від графіка черг — так повідомляє ДТЕК для вашого будинку.</i>")
        elif new and old and not old_sched and new["end"] != old["end"] and new["end"]:
            text = (f"🔄 <b>Змінився час відновлення світла</b>\n{place}\n{html.escape(new['type'])}\n"
                    f"Тепер орієнтовно до: <b>{html.escape(new['end'])}</b>"
                    + (f" (було до {html.escape(old['end'])})" if old["end"] else ""))
        elif old and not new:
            text = (f"✅ <b>ДТЕК завершив роботи за вашою адресою</b>\n{place}\n"
                    f"<i>{html.escape(old['type'])}</i>\n"
                    "Якщо світла досі немає, а за графіком має бути — повідомте ДТЕК.")
        sub["emergency"] = new
        if text:
            log.info("[%s] аварійні роботи за адресою чату %s: %s -> %s", region, chat_id, old, new)
            await _send(ctx, chat_id, sub, text, "emergency")


async def post_init(app: Application):
    await dtek.start()
    await app.bot.set_my_commands([
        BotCommand("now", "Графік на сьогодні й завтра"),
        BotCommand("start", "Вказати або змінити адресу чи чергу"),
        BotCommand("settings", "Нагадування та сповіщення"),
        BotCommand("stop", "Вимкнути сповіщення"),
        BotCommand("help", "Що вміє бот"),
    ])
    # адміністраторам (за числовим ID) — те саме меню плюс /stats
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.set_my_commands(
                [*(await app.bot.get_my_commands()), BotCommand("stats", "Статистика бота"),
                 BotCommand("users", "Люди та їхні адреси")],
                scope=BotCommandScopeChat(admin_id))
        except Exception as e:  # noqa: BLE001 — людина ще не писала боту
            log.info("меню /stats для %s поки не встановлено: %s", admin_id, e)
    # опис бота (порожній чат і профіль) задається вручну в @BotFather: /setdescription, /setabouttext
    app.job_queue.run_repeating(poll, interval=POLL_MINUTES * 60, first=15)
    app.job_queue.run_repeating(remind, interval=60, first=40)
    stats.started(store.data)
    log.info("Бот запущено. Перевірка кожні %d хв, підписок: %d, людей у статистиці: %d (файл %s)",
             POLL_MINUTES, len(store.data), len(stats.data["users"]), stats.path)


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
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
