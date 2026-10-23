"""Розбір і форматування графіків ДТЕК (DisconSchedule.fact).

Формат fact з сайту:
{
  "data": { "<unix ts півночі за Києвом>": { "GPV5.1": {"1": "yes", ..., "24": "no"}, ... } },
  "update": "22.10.2026 21:44",
  "today": 1792702800
}
Година "1" = 00:00–01:00, "24" = 23:00–24:00.
"""
from __future__ import annotations

import html
from datetime import date, datetime
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")

ON, OFF, MAYBE = "1", "0", "?"

# стан години -> (перші 30 хв, другі 30 хв)
HALVES = {
    "yes": (ON, ON),
    "no": (OFF, OFF),
    "first": (OFF, ON),
    "second": (ON, OFF),
    "maybe": (MAYBE, MAYBE),
    "mfirst": (MAYBE, ON),
    "msecond": (ON, MAYBE),
}

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]


def group_label(group: str, names: dict | None = None) -> str:
    if names and group in names:
        return names[group]
    return "Черга " + group.replace("GPV", "")


def hours_to_slots(hours: dict) -> str:
    """24 години -> рядок із 48 півгодинних слотів (ON/OFF/MAYBE)."""
    out = []
    for h in range(1, 25):
        a, b = HALVES.get(str(hours.get(str(h), "yes")), (ON, ON))
        out += [a, b]
    return "".join(out)


def intervals(slots: str, state: str) -> list[tuple[int, int]]:
    """Безперервні проміжки зі станом state, у хвилинах від початку доби."""
    res, start = [], None
    for i, s in enumerate(slots + "#"):
        if s == state and start is None:
            start = i
        elif s != state and start is not None:
            res.append((start * 30, i * 30))
            start = None
    return res


def fmt_min(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def fmt_hours(minutes: int) -> str:
    h = minutes / 60
    return f"{h:g}".replace(".", ",") + " год"


def _data_dict(fact: dict) -> dict:
    data = (fact or {}).get("data") or {}
    return data if isinstance(data, dict) else {}


def fact_days(fact: dict) -> dict[date, dict]:
    """{дата: {черга: {година: стан}}} з переведенням ключів-таймстемпів у дату за Києвом."""
    days = {}
    for ts, groups in _data_dict(fact).items():
        try:
            d = datetime.fromtimestamp(int(ts), KYIV).date()
        except (TypeError, ValueError):
            continue
        if isinstance(groups, dict):
            days[d] = groups
    return dict(sorted(days.items()))


def today_kyiv(now: datetime | None = None) -> date:
    return (now or datetime.now(KYIV)).astimezone(KYIV).date()


def signature(fact: dict, groups: list[str], now: datetime | None = None) -> dict:
    """Відбиток графіка для своїх черг на сьогодні й далі — для порівняння змін."""
    t = today_kyiv(now)
    sig = {}
    for d, gdata in fact_days(fact).items():
        if d < t:
            continue
        sig[d.isoformat()] = {g: hours_to_slots(gdata[g]) for g in groups if g in gdata}
    return sig


def has_changed(old: dict | None, new: dict, now: datetime | None = None) -> bool:
    """Чи змінився графік. Дні, що вже минули, ігноруються (щоб не слати нічого опівночі)."""
    if old is None:
        return False
    t = today_kyiv(now).isoformat()
    keys = {k for k in set(old) | set(new) if k >= t}
    return any(old.get(k) != new.get(k) for k in keys)


def _now_line(slots: str, now: datetime) -> str:
    """Що зараз і коли наступна зміна."""
    minute = now.hour * 60 + now.minute
    i = min(minute // 30, 47)
    cur = slots[i]
    j = i
    while j < 48 and slots[j] == cur:
        j += 1
    until = fmt_min(j * 30) if j < 48 else None
    if cur == OFF:
        return "🔴 Зараз світла немає" + (f" · увімкнуть о {until}" if until else " до кінця доби")
    if cur == MAYBE:
        return "🟡 Зараз можливе відключення" + (f" · до {until}" if until else "")
    nxt = next((k for k in range(j, 48) if slots[k] == OFF), None)
    if nxt is None:
        return "🟢 Зараз світло є · до кінця доби відключень немає"
    return f"🟢 Зараз світло є · відключення о {fmt_min(nxt * 30)}"


def _day_block(slots: str) -> list[str]:
    off = intervals(slots, OFF)
    maybe = intervals(slots, MAYBE)
    lines = []
    if not off and not maybe:
        return ["✅ Відключень немає"]
    for a, b in off:
        lines.append(f"🔴 {fmt_min(a)}–{fmt_min(b)}")
    for a, b in maybe:
        lines.append(f"🟡 {fmt_min(a)}–{fmt_min(b)} (можливо)")
    total = sum(b - a for a, b in off)
    if total:
        lines.append(f"<i>Без світла: {fmt_hours(total)}</i>")
    return lines


def _day_title(d: date, t: date) -> str:
    rel = "Сьогодні" if d == t else "Завтра" if (d - t).days == 1 else d.strftime("%d.%m")
    return f"<b>{rel}, {WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}</b>"


def format_schedule(fact: dict, groups: list[str], label: str = "", names: dict | None = None,
                    now: datetime | None = None, header: str = "⚡️ <b>Графік відключень</b>",
                    extra: str = "") -> str:
    now = (now or datetime.now(KYIV)).astimezone(KYIV)
    t = now.date()
    days = {d: g for d, g in fact_days(fact).items() if d >= t}

    lines = [header]
    if label:
        lines.append("📍 " + html.escape(label))
    if fact and fact.get("update"):
        lines.append(f"🕘 ДТЕК оновив графік: {html.escape(str(fact['update']))}")
    if extra:
        lines.append(extra)

    for g in groups:
        lines.append("")
        if len(groups) > 1 or not label:
            lines.append(f"🔢 <b>{html.escape(group_label(g, names))}</b>")
        elif label:
            lines[1] += f" · {html.escape(group_label(g, names))}"

        today_data = days.get(t, {}).get(g)
        if today_data is not None:
            slots = hours_to_slots(today_data)
            lines.append(_day_title(t, t))
            if OFF in slots or MAYBE in slots:
                lines.append(_now_line(slots, now))
            lines += _day_block(slots)
        else:
            lines.append(_day_title(t, t))
            lines.append("Графіка на сьогодні немає")

        future = [d for d in days if d > t and g in days[d]]
        lines.append("")
        if future:
            for d in future:
                lines.append(_day_title(d, t))
                lines += _day_block(hours_to_slots(days[d][g]))
        else:
            tomorrow_title = "<b>Завтра</b>"
            lines.append(f"{tomorrow_title}: графіка ще немає")
    return "\n".join(lines).replace("\n\n\n", "\n\n")
