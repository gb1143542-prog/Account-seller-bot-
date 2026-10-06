# ============================================================
#  Telegram Account Seller Bot  -  single file (BotHost ready)
#  Files needed: bot.py + requirements.txt + .env
# ============================================================
import asyncio
import csv
import hashlib
import html
import io
import json
import logging
import os
import random
import re
import sys
import time
from urllib.parse import quote

import firebase_admin
import qrcode
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup as M
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from firebase_admin import credentials, db as fdb
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("bot")


def ensure_telethon():
    """telethon/pyaes sirf source package hain, BotHost wheel-only install karta hai.
    Isliye ye start par khud install kar leta hai (pehli baar ~30-60s lagta hai)."""
    try:
        import telethon  # noqa: F401
        return
    except ImportError:
        pass
    import importlib
    import subprocess
    import tempfile
    target = os.path.join(tempfile.gettempdir(), "botlibs")
    os.makedirs(target, exist_ok=True)
    if target not in sys.path:
        sys.path.insert(0, target)
    importlib.invalidate_caches()
    try:
        import telethon  # noqa: F401
        return
    except ImportError:
        pass
    LOG.info("Installing telethon (first start only)...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "--no-warn-script-location",
                               "--disable-pip-version-check", "--target", target,
                               "telethon==1.37.0", "pyaes==1.6.1", "rsa"])
    except Exception as e:
        LOG.error("Telethon install fail hua: %s", e)
        sys.exit(1)
    importlib.invalidate_caches()


ensure_telethon()
from telethon import TelegramClient  # noqa: E402
from telethon.errors import (FloodWaitError, PasswordHashInvalidError, PhoneCodeExpiredError,  # noqa: E402
                             PhoneCodeInvalidError, PhoneNumberInvalidError, SessionPasswordNeededError)
from telethon.sessions import StringSession  # noqa: E402

# ==================== CONFIG ====================
load_dotenv()


def env(k, d=""):
    v = os.getenv(k, d).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1].strip()
    return v


def env_ints(k):
    return [int(x) for x in env(k).replace(" ", "").split(",") if x.lstrip("-").isdigit()]


def env_float(k, d):
    try:
        return float(env(k, str(d)))
    except ValueError:
        return float(d)


def parse_table(s):
    out = []
    for p in s.split(","):
        if ":" in p:
            a, b = p.split(":", 1)
            try:
                out.append((float(a), float(b)))
            except ValueError:
                pass
    return sorted(out)


BOT_TOKEN = env("BOT_TOKEN")
API_ID = int(env("API_ID", "0") or 0)
API_HASH = env("API_HASH")
ADMIN_IDS = env_ints("ADMIN_IDS")
FIREBASE_DB_URL = env("FIREBASE_DB_URL")
FIREBASE_CREDENTIALS_JSON = env("FIREBASE_CREDENTIALS_JSON")
ENCRYPTION_KEY = env("ENCRYPTION_KEY")
LOG_CHANNEL_ID = int(env("LOG_CHANNEL_ID", "0") or 0)
FEED_CHANNEL_ID = int(env("FEED_CHANNEL_ID", "0") or 0)
FORCE_JOIN = [x.strip() for x in env("FORCE_JOIN").split(",") if x.strip()]
UPI_ID = env("UPI_ID")
UPI_NAME = env("UPI_NAME", "Store")
MIN_DEPOSIT = env_float("MIN_DEPOSIT", 10)
OTP_WINDOW_MIN = int(env_float("OTP_WINDOW_MIN", 15))
SUPPORT_USERNAME = env("SUPPORT_USERNAME").lstrip("@")
BOT_NAME = env("BOT_NAME", "Account Store")
REF_BONUS = env_float("REF_BONUS", 1)
REF_ON = env("REF_ON", "deposit").lower()
LOW_STOCK = int(env_float("LOW_STOCK", 3))
BUTTON_ICONS = env("BUTTON_ICONS", "0").lower()   # 0 = buttons me normal emoji (hamesha dikhte hain)
GMAIL_USER = env("GMAIL_USER")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD").replace(" ", "")
PAY_MAIL_DOMAINS = [d.strip().lower() for d in env("PAY_MAIL_DOMAINS", "famapp.in,fampay.in").split(",") if d.strip()]
MAIL_REQUIRE_DKIM = env("MAIL_REQUIRE_DKIM", "1") != "0"
MAIL_POLL_SEC = max(10, int(env_float("MAIL_POLL_SEC", 20)))
AUTO_MAX = env_float("AUTO_MAX", 5000)
CREDIT_WORDS = [w.strip().lower() for w in env("MAIL_CREDIT_WORDS", "received,credited,credit").split(",") if w.strip()]
VIP_TABLE = parse_table(env("VIP_TIERS", "1000:2,5000:5,20000:8"))
BULK_TABLE = parse_table(env("BULK_TIERS", "3:3,5:5"))
VIP_NAMES = ["Member", "Bronze", "Silver", "Gold", "Platinum", "Diamond"]


BOT_USER = {"n": "", "id": 0}


def check_config():
    need = ["BOT_TOKEN", "API_ID", "API_HASH", "ADMIN_IDS", "FIREBASE_DB_URL", "FIREBASE_CREDENTIALS_JSON", "ENCRYPTION_KEY"]
    missing = [k for k in need if not env(k) or env(k) == "0"]
    if missing:
        LOG.error("Missing .env values: %s", ", ".join(missing))
        sys.exit(1)


# ==================== DATABASE ====================
_fernet = None


def init_services():
    global _fernet
    raw = FIREBASE_CREDENTIALS_JSON
    info = None
    if raw.lower().endswith(".json"):
        here = os.path.dirname(os.path.abspath(__file__))
        for path in (raw, os.path.join(here, raw)):
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    info = json.load(fh)
                break
        if info is None:
            LOG.error("Firebase JSON file '%s' bot.py ke saath upload nahi hui.", raw)
            sys.exit(1)
    else:
        info = json.loads(raw, strict=False)
    firebase_admin.initialize_app(credentials.Certificate(info), {"databaseURL": FIREBASE_DB_URL})
    _fernet = Fernet(ENCRYPTION_KEY.encode())


def enc(s):
    return _fernet.encrypt(s.encode()).decode()


def dec(s):
    return _fernet.decrypt(s.encode()).decode()


async def _run(fn):
    return await asyncio.get_running_loop().run_in_executor(None, fn)


async def dget(p):
    return await _run(lambda: fdb.reference(p).get())


async def dset(p, v):
    await _run(lambda: fdb.reference(p).set(v))


async def dupd(p, v):
    await _run(lambda: fdb.reference(p).update(v))


async def dpush(p, v):
    return await _run(lambda: fdb.reference(p).push(v).key)


async def ddel(p):
    await _run(lambda: fdb.reference(p).delete())


async def dtx(p, fn):
    return await _run(lambda: fdb.reference(p).transaction(fn))


async def dkeys(p):
    r = await _run(lambda: fdb.reference(p).get(shallow=True))
    return list(r.keys()) if isinstance(r, dict) else []


async def dincr(p, n):
    def f(cur):
        return round((cur or 0) + n, 2)
    return await dtx(p, f)


async def add_bal(uid, amt):
    return await dincr(f"users/{uid}/balance", amt)


async def ded_bal(uid, amt):
    box = {"ok": False}

    def f(cur):
        cur = cur or 0
        if cur < amt:
            box["ok"] = False
            return cur
        box["ok"] = True
        return round(cur - amt, 2)

    await dtx(f"users/{uid}/balance", f)
    return box["ok"]


async def claim_once(path, value=True):
    """Atomically set path if empty. True if we got it."""
    box = {"ok": False}

    def f(cur):
        if cur is None:
            box["ok"] = True
            return value
        box["ok"] = False
        return cur

    await dtx(path, f)
    return box["ok"]


def now():
    return int(time.time())


def day_key(ts=None):
    return time.strftime("%Y%m%d", time.gmtime((ts or now()) + 19800))  # IST


async def bump_stats(**kw):
    dk = day_key()
    for k, v in kw.items():
        if v:
            await dincr(f"stats/{k}", v)
            await dincr(f"stats/daily/{dk}/{k}", v)


async def ensure_user(u):
    d = await dget(f"users/{u.id}")
    if d is None:
        d = {"name": u.full_name, "username": u.username or "", "balance": 0, "spent": 0,
             "orders": 0, "banned": False, "joinedAt": now(), "lang": "hi"}
        await dset(f"users/{u.id}", d)
        d = dict(d)
        d["_new"] = True
    return d


# ==================== UTILS / ROLES ====================
def esc(s):
    return html.escape(str(s if s is not None else ""))


def money(x):
    s = "₹{:,.2f}".format(float(x or 0))
    return s.rstrip("0").rstrip(".")


async def show(cb, text, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        try:
            await cb.message.delete()
        except Exception:
            pass
        await cb.message.answer(text, reply_markup=markup)


async def notify(bot, uid, text, markup=None):
    try:
        await bot.send_message(uid, text, reply_markup=markup)
        return True
    except Exception:
        return False


async def tolog(bot, text):
    if LOG_CHANNEL_ID:
        try:
            await bot.send_message(LOG_CHANNEL_ID, text)
        except Exception as e:
            LOG.warning("log channel error: %s", e)


def mask_id(uid):
    s = str(uid)
    return s[:2] + "•" * max(len(s) - 4, 2) + s[-2:] if len(s) > 4 else "•••"


def mask_name(name):
    return esc((name or "User")[:2]) + "•••"


_feed_tasks = set()


async def _feed_send(bot, text):
    try:
        ids = await feed_ids()
        if not ids:
            return
        text = re.sub(r"\+\d{8,15}", lambda m: mask_phone(m.group(0)), text)
        markup = None
        if BOT_USER.get("n"):
            markup = kb([[B(text="🤖 Open Bot", url="https://t.me/" + BOT_USER["n"])]])
        for cid in ids:
            try:
                await bot.send_message(cid, text, reply_markup=markup)
            except Exception as e:
                LOG.warning("feed channel %s error: %s", cid, e)
    except Exception as e:
        LOG.warning("feed error: %s", e)


def feed(bot, text):
    """Public feed post (masked details). Background me chalta hai."""
    if bot is None:
        return
    task = asyncio.create_task(_feed_send(bot, text))
    _feed_tasks.add(task)
    task.add_done_callback(_feed_tasks.discard)


RANK = {"support": 1, "manager": 2, "owner": 3}
_admins = {"t": 0.0, "d": {}}


async def load_admins(force=False):
    if force or time.monotonic() - _admins["t"] > 30:
        d = await dget("admins") or {}
        _admins["d"] = {int(k): v for k, v in d.items() if str(k).isdigit() and v in RANK}
        _admins["t"] = time.monotonic()


def role_of(uid):
    if uid in ADMIN_IDS:
        return "owner"
    return _admins["d"].get(uid)


async def staff_ids(level="support"):
    await load_admins()
    ids = set(ADMIN_IDS)
    for k, v in _admins["d"].items():
        if RANK[v] >= RANK[level]:
            ids.add(k)
    return list(ids)


class Staff(BaseFilter):
    def __init__(self, level="support"):
        self.level = RANK[level]

    async def __call__(self, event):
        u = event.from_user
        if not u:
            return False
        await load_admins()
        r = role_of(u.id)
        return bool(r) and RANK[r] >= self.level


def kb(rows):
    return M(inline_keyboard=rows)


def csv_file(name, header, rows):
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(header)
    w.writerows(rows)
    return BufferedInputFile(s.getvalue().encode("utf-8-sig"), name)


# ==================== PREMIUM EMOJI ====================
PE_POOL = [
    "6336646834139700626",
    "6336861449360514102",
    "6337033209397649451",
    "6336698133229082903",
    "6336674562448563935",
    "6100639476441161711",
    "6102462664288509137",
    "6100199534351097095",
    "6102926404792360795",
    "6100409966273764915",
    "6100430105375415737",
    "6102470558438400435",
    "6100451820730064687",
    "6102638599033858630",
    "6100179369479642954",
    "6100485115316542792",
    "6102661242101440205",
    "6102592514034770678",
    "6102475626499808862",
    "6102863908723236868",
    "6102510630483271620",
    "6282589525348720171",
    "6055377380204092112",
    "6055551219005398825",
    "6055181976371994390",
    "6055481009175010794",
    "6055484548228062462",
    "6055202102588742236",
    "6055450347403484860",
    "6055228576767155521",
    "6055183995006623379",
    "6337009415278828759",
    "6336756235546663929",
    "6334772471757020134",
    "6336732269629153634",
    "6336833407519038409",
    "6337048276142924106",
    "6337018975876030803",
    "6336608132189395373",
    "6336797785060286399",
    "6336685231147326793",
    "6336907611669011898",
    "6336988189550451848",
    "6337098578799893838",
    "6336808092981796477",
    "6337020083977592163",
    "6337112997005107243",
    "6337051755066433311",
    "6336835907190004485",
    "6336618976981818626",
    "6336857218817728795",
    "6336974471424908889",
    "6337125748763008448",
    "6337098338281725706",
    "6336962978092425393",
    "6336633214798404108",
    "6337019139084786234",
    "6337035356881296575",
    "6337026908680625329",
    "6336690569791676356",
    "6337106906741480828",
    "6337072645787361389",
    "6336720729052027967",
    "6336670885956557643",
    "6337113894653271580",
    "6334488003188105980",
    "6336721798498884548",
    "6336799284003873851",
    "6337112129421713282",
    "6336599202952388231",
    "6336755629956275338",
    "6334702021408465964",
    "6337109865973948062",
    "6336708763273142215",
    "6337083451925078342",
    "6336930400765484501",
    "6334788126912815244",
    "6337059606266651217",
    "6336812005697002754",
    "6336813629194640485",
    "6337085796977221633",
    "6336663202260065128",
    "6334324468013341494",
    "6337047855236129713",
    "6336782885818742144",
    "6336664645369076808",
    "6336910583786383660",
    "6336862179504954500",
    "6336697226990985005",
    "6336772620846899242",
    "6336573617832206335",
    "6337055242579876765",
    "6336789422758960593",
    "6336781331040577785",
    "6336603218746810844",
    "6337123072998383823",
    "6336894825551371014",
    "6334681658968513467",
    "6336799919659031563",
    "6336707603631972035",
    "6336874467406389346",
    "6336756411640323933",
    "6336608037700115865",
    "6336613247495445753",
    "6336973539417007164",
    "6336931040715612818",
    "6336653869296132233",
    "6336836572909938734",
    "6336798231736885254",
    "6336813951317187443",
    "6336866435817545002",
    "6336662845777780692",
    "6336580455420141312",
    "6336750437340816001",
    "6336677470141422007",
    "6337078718871117522",
    "6336931345658289868",
    "6336935322798005307",
    "6337010179783007229",
    "6336618208182673162",
    "6336580975111184057",
    "6336957184181543528",
    "6336991256157101601",
    "6336655355354815762",
    "6336795865209904645",
    "6337054177427988529",
    "6336855354801921798",
    "6336878444546105899",
    "6336861037043654967",
    "6336662472115626382",
    "6337093386184432717",
    "6336637947852365586",
    "6336876696494417749",
    "6334678278829252492",
    "6337087411884923105",
    "6336989731443711607",
    "6336882614959349480",
    "6336886055228153516",
    "6336797591786757523",
    "6336674519498890396",
    "6336856849450540332",
    "6337048379222138619",
    "6336816932024491505",
    "6336672814396874442",
    "6336835035311644293",
    "6336668004033504924",
    "6336682357814205676",
    "6336764563488252026",
    "6337100812182887680",
    "6336575056646249129",
]
PE_MAP = {
    "✅": "6336861449360514102", "❌": "6337033209397649451", "📢": "6336698133229082903",
    "📊": "6336674562448563935", "⭐": "6336646834139700626", "🚀": "6336674562448563935",
}
PREMIUM = {"on": True, "ok": 0, "fail": 0, "btn": False}
NO_PREMIUM = set()   # channels: custom emoji wahan allowed nahi, plain emoji jayenge
_FLAG = r"[\U0001F1E6-\U0001F1FF]{2}"
EMO_RE = re.compile(
    r"<tg-emoji[^>]*>.*?</tg-emoji>|" + _FLAG +
    r"|[\U0001F000-\U0001FAFF]\uFE0F?"
    r"|[\u2705\u274C\u2B50\u26A1\u23F3\u231B\u2728\u2753\u2757\u2795\u2796\u2797]\uFE0F?"
    r"|[\u2190-\u21FF\u2300-\u23FF\u2600-\u27BF\u2900-\u297F\u2B00-\u2BFF\u2139\u24C2\u203C\u2049]\uFE0F",
    re.S)
_FLAG_RE = re.compile("^" + _FLAG + "$")


def pe_id(ch):
    base = ch.replace("\ufe0f", "")
    return PE_MAP.get(base) or PE_POOL[int(hashlib.md5(base.encode()).hexdigest(), 16) % len(PE_POOL)]


def premium_text(txt):
    def f(m):
        g = m.group(0)
        if g.startswith("<tg-emoji") or _FLAG_RE.match(g):
            return g
        return '<tg-emoji emoji-id="{}">{}</tg-emoji>'.format(pe_id(g), g)
    return EMO_RE.sub(f, txt)


def premium_markup(mk):
    changed, rows = False, []
    for row in mk.inline_keyboard:
        nr = []
        for b in row:
            st = (b.text or "").lstrip()
            m = EMO_RE.match(st)
            if m and not m.group(0).startswith("<tg-emoji") and not _FLAG_RE.match(m.group(0)):
                rest = st[m.end():].lstrip()
                if rest:
                    d = b.model_dump(exclude_none=True)
                    d["text"] = rest
                    d["icon_custom_emoji_id"] = pe_id(m.group(0))
                    nr.append(B(**d))
                    changed = True
                    continue
            nr.append(b)
        rows.append(nr)
    return M(inline_keyboard=rows) if changed else None


def apply_premium(method):
    if not PREMIUM["on"] or getattr(method, "parse_mode", None) is None:
        return None
    if getattr(method, "chat_id", None) in NO_PREMIUM:
        return None
    saved = {}
    for f in ("text", "caption"):
        v = getattr(method, f, None)
        if isinstance(v, str) and v:
            nv = premium_text(v)
            if nv != v:
                saved[f] = v
                setattr(method, f, nv)
    mk = getattr(method, "reply_markup", None)
    if isinstance(mk, M) and PREMIUM["btn"]:
        nm = premium_markup(mk)
        if nm is not None:
            saved["reply_markup"] = mk
            method.reply_markup = nm
    return saved or None


class SafeBot(Bot):
    """Har outgoing message/button me premium emoji lagata hai. Telegram reject kare to plain emoji se resend."""
    async def __call__(self, method, request_timeout=None):
        saved = apply_premium(method)
        try:
            res = await super().__call__(method, request_timeout=request_timeout)
            if saved:
                PREMIUM["ok"] += 1
            return res
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if saved and any(k in msg for k in ("emoji", "entit", "icon", "button")):
                PREMIUM["fail"] += 1
                if "icon" in msg or "button" in msg:
                    PREMIUM["btn"] = False
                if PREMIUM["fail"] >= 5 and PREMIUM["ok"] == 0:
                    PREMIUM["on"] = False
                    LOG.warning("Premium emoji kaam nahi kar rahe, plain emoji use honge.")
                for k, v in saved.items():
                    setattr(method, k, v)
                return await super().__call__(method, request_timeout=request_timeout)
            raise


async def build_premium(bot):
    """Pool ke emoji ka asli emoji-type Telegram se pooch kar match karta hai (🛒 -> shopping wala etc)."""
    try:
        for i in range(0, len(PE_POOL), 200):
            for st in await bot.get_custom_emoji_stickers(PE_POOL[i:i + 200]):
                base = (st.emoji or "").replace("\ufe0f", "")
                if base and base not in PE_MAP:
                    PE_MAP[base] = st.custom_emoji_id
        LOG.info("Premium emoji mapped: %d", len(PE_MAP))
    except Exception as e:
        LOG.warning("Premium emoji map nahi bana (%s), random pool use hoga.", type(e).__name__)


async def calibrate_buttons(bot):
    """Button me premium icon sach me lagta hai ya nahi, test se pata karta hai.
    Na lage to normal emoji button text me rehte hain (button se emoji gayab nahi hoga)."""
    if BUTTON_ICONS == "1":
        PREMIUM["btn"] = True
        return
    PREMIUM["btn"] = False
    if BUTTON_ICONS == "0" or not ADMIN_IDS:
        return
    try:
        test = M(inline_keyboard=[[B(text="Test", callback_data="noop", icon_custom_emoji_id=PE_MAP.get("✅") or PE_POOL[0])]])
        res = await bot.send_message(ADMIN_IDS[0], "Button check...", reply_markup=test, disable_notification=True)
        ok = any(getattr(b, "icon_custom_emoji_id", None) for row in (res.reply_markup.inline_keyboard if res.reply_markup else []) for b in row)
        try:
            await bot.delete_message(ADMIN_IDS[0], res.message_id)
        except Exception:
            pass
        PREMIUM["btn"] = bool(ok)
        LOG.info("Button premium icons: %s", "ON" if ok else "OFF (plain emoji use honge)")
    except Exception as e:
        LOG.warning("Button icon check fail (%s), plain emoji use honge.", type(e).__name__)


# ==================== SETTINGS (admin panel se editable) ====================
_cfg = {"t": 0.0, "d": {}}


def refresh_no_premium():
    ids = set()
    ch = _cfg["d"].get("channels") or {}
    for grp in ("force", "feed"):
        for v in (ch.get(grp) or {}).values():
            ids.add(v.get("id"))
    if LOG_CHANNEL_ID:
        ids.add(LOG_CHANNEL_ID)
    if FEED_CHANNEL_ID:
        ids.add(FEED_CHANNEL_ID)
    NO_PREMIUM.clear()
    NO_PREMIUM.update(i for i in ids if i is not None)


async def cfg(force=False):
    if force or time.monotonic() - _cfg["t"] > 20:
        _cfg["d"] = await dget("settings") or {}
        _cfg["t"] = time.monotonic()
        refresh_no_premium()
    return _cfg["d"]


async def upi_info():
    u = (await cfg()).get("upi") or {}
    return (u.get("id") or UPI_ID), (u.get("name") or UPI_NAME)


async def force_list():
    out = []
    for v in ((await cfg()).get("channels") or {}).get("force", {}).values():
        out.append({"id": v["id"], "title": v.get("title", ""), "link": v.get("link")})
    for ch in FORCE_JOIN:
        out.append({"id": ch, "title": ch, "link": ("https://t.me/" + ch.lstrip("@")) if ch.startswith("@") else None})
    return out


async def feed_ids():
    ids = [v["id"] for v in ((await cfg()).get("channels") or {}).get("feed", {}).values()]
    if FEED_CHANNEL_ID and FEED_CHANNEL_ID not in ids:
        ids.append(FEED_CHANNEL_ID)
    return ids


def mask_phone(phone):
    d = re.sub(r"\D", "", phone or "")
    if len(d) <= 4:
        return "+••••"
    return "+" + d[:2] + "•" * (len(d) - 4) + d[-2:]


# ==================== LANGUAGE ====================
TX = {
    "hi": {
        "welcome": "👋 <b>Welcome to {name}</b>\n\n⚡ Instant auto delivery\n🔐 Auto OTP system\n💳 Easy wallet top-up\n\nNeeche se option choose karo 👇",
        "b_buy": "🛒 Buy Account", "b_wallet": "💰 Wallet", "b_orders": "📦 My Orders", "b_profile": "👤 Profile",
        "b_ref": "👥 Referral", "b_promo": "🏷 Promo Code", "b_top": "🏆 Leaderboard",
        "b_support": "🆘 Support", "b_lang": "🌐 Language", "b_admin": "⚙️ Admin Panel", "b_back": "⬅️ Back",
        "join": "🔒 Pehle neeche ke channels join karo:",
    },
    "en": {
        "welcome": "👋 <b>Welcome to {name}</b>\n\n⚡ Instant auto delivery\n🔐 Auto OTP system\n💳 Easy wallet top-up\n\nChoose an option below 👇",
        "b_buy": "🛒 Buy Account", "b_wallet": "💰 Wallet", "b_orders": "📦 My Orders", "b_profile": "👤 Profile",
        "b_ref": "👥 Referral", "b_promo": "🏷 Promo Code", "b_top": "🏆 Leaderboard",
        "b_support": "🆘 Support", "b_lang": "🌐 Language", "b_admin": "⚙️ Admin Panel", "b_back": "⬅️ Back",
        "join": "🔒 Please join the channels below first:",
    },
}


def L(u):
    return (u or {}).get("lang", "hi") if (u or {}).get("lang") in TX else "hi"


def t(lang, key):
    return TX.get(lang, TX["hi"]).get(key, key)


def main_menu(lang, uid):
    rows = [
        [B(text=t(lang, "b_buy"), callback_data="menu:buy"), B(text=t(lang, "b_wallet"), callback_data="menu:wallet")],
        [B(text=t(lang, "b_orders"), callback_data="menu:orders"), B(text=t(lang, "b_profile"), callback_data="menu:profile")],
        [B(text=t(lang, "b_ref"), callback_data="menu:ref"), B(text=t(lang, "b_promo"), callback_data="menu:promo")],
        [B(text=t(lang, "b_top"), callback_data="menu:top"), B(text=t(lang, "b_support"), callback_data="sup:menu")],
        [B(text=t(lang, "b_lang"), callback_data="lang:menu")],
    ]
    if role_of(uid):
        rows.append([B(text=t(lang, "b_admin"), callback_data="adm:main")])
    return kb(rows)


def back(lang="hi", to="menu:main"):
    return kb([[B(text=t(lang, "b_back"), callback_data=to)]])


# ==================== PRICING ====================
def pct_for(table, value):
    p = 0.0
    for th, pc in table:
        if value >= th:
            p = pc
    return p


def vip_info(spent):
    idx, pct = 0, 0.0
    for i, (th, pc) in enumerate(VIP_TABLE, 1):
        if (spent or 0) >= th:
            idx, pct = i, pc
    return VIP_NAMES[min(idx, len(VIP_NAMES) - 1)], pct


async def global_discount():
    d = await dget("settings/discount") or {}
    try:
        pct = float(d.get("pct", 0) or 0)
        until = int(d.get("until", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if pct > 0 and (until == 0 or now() < until):
        return pct
    return 0.0


async def calc_price(base, qty, spent):
    _, vip = vip_info(spent)
    bulk = pct_for(BULK_TABLE, qty)
    g = await global_discount()
    pct = min(vip + bulk + g, 60.0)
    unit = max(round(float(base) * (1 - pct / 100), 2), 0.01)
    return unit, pct


# ==================== OTP ENGINE (Telethon) ====================
CODE_RE = re.compile(r"(?<!\d)(\d{5,6})(?!\d)")
DEAD_ERRORS = {"AuthKeyUnregisteredError", "UserDeactivatedError", "UserDeactivatedBanError",
               "SessionRevokedError", "SessionExpiredError", "AuthKeyError", "AuthKeyDuplicatedError"}


def _tg_client(sess):
    return TelegramClient(StringSession(sess), API_ID, API_HASH, connection_retries=2)


async def _close(c):
    try:
        await c.disconnect()
    except Exception:
        pass


async def health_check(sess):
    c = _tg_client(sess)
    try:
        await asyncio.wait_for(c.connect(), 30)
        if not await c.is_user_authorized():
            return None
        me = await c.get_me()
        return ("+" + me.phone) if me and me.phone else None
    except Exception as e:
        LOG.warning("health_check: %s", e)
        return None
    finally:
        await _close(c)


async def fetch_code(sess, since):
    c = _tg_client(sess)
    try:
        await asyncio.wait_for(c.connect(), 30)
        if not await c.is_user_authorized():
            return {"status": "dead"}
        try:
            msgs = await c.get_messages(777000, limit=5)
        except ValueError:
            await c.get_dialogs(limit=30)
            msgs = await c.get_messages(777000, limit=5)
        for m in msgs:
            if m.date.timestamp() < since:
                break
            mt = CODE_RE.search(m.raw_text or "")
            if mt:
                return {"status": "ok", "code": mt.group(1), "id": m.id, "date": int(m.date.timestamp())}
        return {"status": "none"}
    except Exception as e:
        LOG.warning("fetch_code: %s", e)
        if type(e).__name__ in DEAD_ERRORS:
            return {"status": "dead"}
        return {"status": "error"}
    finally:
        await _close(c)


async def logout_session(sess):
    """Kills the bot's own session so only the buyer's login stays."""
    c = _tg_client(sess)
    try:
        await asyncio.wait_for(c.connect(), 30)
        await c.log_out()
    except Exception as e:
        LOG.warning("logout_session: %s", e)
    finally:
        await _close(c)


# ==================== STOCK ====================
def slug(name):
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30]
    return s or hashlib.md5(name.encode()).hexdigest()[:8]


async def add_item(country_name, price, phone, sess, tfa, desc=""):
    digits = re.sub(r"\D", "", phone)
    ex = await dget(f"phones/{digits}")
    if ex:
        st = await dget(f"stock/{ex}/status")
        if st in ("available", "locked"):
            return "dup"
    ck = slug(country_name)
    await dupd(f"countries/{ck}", {"name": country_name.strip(), "price": price})
    item = {"phone": phone, "sess": enc(sess), "status": "available", "addedAt": now()}
    if tfa:
        item["tfa"] = enc(tfa)
    if desc:
        item["desc"] = desc
    sid = await dpush(f"stock/{ck}", item)
    await dset(f"available/{ck}/{sid}", True)
    await dset(f"phones/{digits}", f"{ck}/{sid}")
    return "ok"


async def lock_stock(ck, uid):
    avail = await dget(f"available/{ck}")
    if not avail:
        return None
    for sid in list(avail.keys()):
        box = {"ok": False}

        def f(cur):
            if cur and cur.get("status") == "available":
                cur["status"] = "locked"
                cur["lockedBy"] = uid
                cur["lockedAt"] = now()
                box["ok"] = True
            else:
                box["ok"] = False
            return cur

        await dtx(f"stock/{ck}/{sid}", f)
        await ddel(f"available/{ck}/{sid}")
        if box["ok"]:
            return sid
    return None


# ==================== ORDERS ====================
STATUS = {"waiting_otp": "⏳ Waiting for login", "completed": "✅ Completed", "refunded": "↩️ Refunded"}


async def create_order(uid, ck, country, sid, phone, price, desc=""):
    exp = now() + OTP_WINDOW_MIN * 60
    o = {"uid": uid, "ckey": ck, "country": country.get("name", ck), "sid": sid, "phone": phone,
         "price": price, "status": "waiting_otp", "createdAt": now(), "otpExpiry": exp,
         "otpCount": 0, "otpDelivered": False}
    if desc:
        o["desc"] = desc
    oid = await dpush("orders", o)
    await dset(f"user_orders/{uid}/{oid}", True)
    await dset(f"active_orders/{oid}", exp)
    return oid, o


async def _claim(oid, new, allowed=("waiting_otp",)):
    box = {"prev": None}

    def f(cur):
        if cur and cur.get("status") in allowed:
            box["prev"] = cur["status"]
            cur["status"] = new
        else:
            box["prev"] = None
        return cur

    await dtx(f"orders/{oid}", f)
    return box["prev"]


async def complete_order(bot, oid):
    if not await _claim(oid, "completed"):
        return False
    o = await dget(f"orders/{oid}")
    sp = f"stock/{o['ckey']}/{o['sid']}"
    st = await dget(sp)
    if st and st.get("sess"):
        await logout_session(dec(st["sess"]))
    await dupd(sp, {"status": "sold", "sess": None, "tfa": None, "soldAt": now()})
    await dupd(f"orders/{oid}", {"completedAt": now()})
    await ddel(f"active_orders/{oid}")
    await dincr(f"users/{o['uid']}/orders", 1)
    await dincr(f"users/{o['uid']}/spent", o["price"])
    await bump_stats(orders=1, revenue=o["price"])
    await notify(bot, o["uid"], ("🎉 <b>Account Buy Successful!</b>\n\n🌍 {}\n💰 {}\n\nThanks for support ❤️\n"
                                 "Security ke liye account ka password/2FA badal lo.").format(esc(o["country"]), money(o["price"])))
    await tolog(bot, "✅ Order completed\n🧾 <code>{}</code>\n👤 <code>{}</code>\n🌍 {}\n📞 {}\n💰 {}".format(
        oid, o["uid"], esc(o["country"]), mask_phone(o["phone"]), money(o["price"])))
    return True


async def refund_order(bot, oid, reason, dead=False, force=False):
    allowed = ("waiting_otp", "completed") if force else ("waiting_otp",)
    prev = await _claim(oid, "refunded", allowed)
    if not prev:
        return False
    o = await dget(f"orders/{oid}")
    await add_bal(o["uid"], o["price"])
    if prev == "waiting_otp":
        sp = f"stock/{o['ckey']}/{o['sid']}"
        if dead:
            await dupd(sp, {"status": "dead"})
        else:
            await dupd(sp, {"status": "available", "lockedBy": None, "lockedAt": None})
            await dset(f"available/{o['ckey']}/{o['sid']}", True)
        await ddel(f"active_orders/{oid}")
    else:
        await bump_stats(orders=-1, revenue=-o["price"])
        await dincr(f"users/{o['uid']}/orders", -1)
        await dincr(f"users/{o['uid']}/spent", -o["price"])
    await notify(bot, o["uid"], "↩️ <b>Order refunded</b>\n{}\n💰 {} wallet me wapas add ho gaya.".format(esc(reason), money(o["price"])))
    await tolog(bot, "↩️ Refund\n🧾 <code>{}</code>\n👤 <code>{}</code>\n{}\n💰 {}".format(oid, o["uid"], esc(reason), money(o["price"])))
    return True


async def replace_order(bot, oid, uid):
    box = {"ok": False}

    def f(cur):
        if (cur and cur.get("uid") == uid and cur.get("status") == "waiting_otp"
                and not cur.get("otpDelivered") and not cur.get("replaced")):
            cur["replaced"] = 1
            box["ok"] = True
        else:
            box["ok"] = False
        return cur

    await dtx(f"orders/{oid}", f)
    if not box["ok"]:
        return "no", None
    o = await dget(f"orders/{oid}")
    nsid = await lock_stock(o["ckey"], uid)
    if not nsid:
        await refund_order(bot, oid, "Replacement stock available nahi hai, refund kar diya.", dead=True)
        return "refund", None
    await dupd(f"stock/{o['ckey']}/{o['sid']}", {"status": "dead"})
    st = await dget(f"stock/{o['ckey']}/{nsid}")
    exp = now() + OTP_WINDOW_MIN * 60
    upd = {"sid": nsid, "phone": st["phone"], "createdAt": now(), "otpExpiry": exp, "otpCount": 0, "desc": st.get("desc")}
    await dupd(f"orders/{oid}", upd)
    await dset(f"active_orders/{oid}", exp)
    o.update(upd)
    return "ok", o


async def watcher(bot):
    while True:
        try:
            act = await dget("active_orders") or {}
            for oid, exp in act.items():
                if now() <= exp:
                    continue
                o = await dget(f"orders/{oid}")
                if not o or o.get("status") != "waiting_otp":
                    await ddel(f"active_orders/{oid}")
                    continue
                if o.get("otpDelivered"):
                    await complete_order(bot, oid)
                else:
                    await refund_order(bot, oid, "OTP window expire ho gayi.")
        except Exception as e:
            LOG.exception("watcher error: %s", e)
        await asyncio.sleep(60)


async def send_backup(bot, targets):
    data = await dget("/")
    raw = json.dumps(data or {}, ensure_ascii=False).encode()
    for a in targets:
        try:
            await bot.send_document(a, BufferedInputFile(raw, "backup_{}.json".format(day_key())), caption="🗄 Database backup")
        except Exception as e:
            LOG.warning("backup send failed: %s", e)


async def backup_loop(bot):
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            await send_backup(bot, ADMIN_IDS)
        except Exception as e:
            LOG.exception("backup error: %s", e)


# ==================== AUTO PAYMENT (Gmail / FamPay mail) ====================
import email  # noqa: E402
import imaplib  # noqa: E402
from email.header import decode_header, make_header  # noqa: E402
from email.utils import parsedate_to_datetime  # noqa: E402

MAIL_ON = bool(GMAIL_USER and GMAIL_APP_PASSWORD)
MAILSTAT = {"last": 0, "err": "", "seen": 0, "recent": [], "others": []}
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _from_domain(frm):
    m = re.search(r"@([\w.\-]+)", frm or "")
    return m.group(1).lower() if m else ""


def _dkim_ok(auth, from_dom):
    """Fake (spoofed) mail se bachne ke liye: Gmail ka DKIM=pass aur sender domain match hona chahiye."""
    for seg in re.findall(r"dkim=pass[^;]*", (auth or "").lower()):
        d = re.search(r"header\.(?:d|i)=@?([\w.\-]+)", seg)
        if d and from_dom:
            dom = d.group(1)
            if from_dom == dom or from_dom.endswith("." + dom) or dom.endswith("." + from_dom):
                return True
    return False


def parse_amounts(text):
    vals = set()
    for m in re.finditer(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)", text, re.I):
        try:
            vals.add(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    for m in re.finditer(r"(?<![\d.])([\d,]*\d\.\d{2})(?!\d)", text):
        try:
            vals.add(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return sorted(vals)


def _mail_text(msg):
    parts = []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct in ("text/plain", "text/html") and not part.get_filename():
            payload = part.get_payload(decode=True) or b""
            try:
                t = payload.decode(part.get_content_charset() or "utf-8", "ignore")
            except LookupError:
                t = payload.decode("utf-8", "ignore")
            if ct == "text/html":
                t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
                t = html.unescape(re.sub(r"<[^>]+>", " ", t))
            parts.append(t)
    return re.sub(r"\s+", " ", " ".join(parts))


def _parse_email(raw):
    msg = email.message_from_bytes(raw)

    def hdr(name):
        try:
            return str(make_header(decode_header(msg.get(name, "") or "")))
        except Exception:
            return msg.get(name, "") or ""

    mid = msg.get("Message-ID") or hashlib.sha1(raw).hexdigest()
    try:
        ts = int(parsedate_to_datetime(msg.get("Date")).timestamp())
    except Exception:
        ts = now()
    return {"key": hashlib.sha1(mid.encode()).hexdigest()[:20], "from": hdr("From"), "subj": hdr("Subject"), "ts": ts,
            "auth": " ".join(msg.get_all("Authentication-Results") or []), "text": hdr("Subject") + " " + _mail_text(msg)}


def dom_allowed(dom, allowed):
    return bool(dom) and any(dom == d or dom.endswith("." + d) for d in allowed)


def _imap_new(last_uid, allowed):
    """Naye mails padhta hai. Pehle sirf header se sender dekhta hai, allowed domain ho tabhi poora mail fetch karta hai."""
    box = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    try:
        box.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        box.select("INBOX", readonly=True)
        tm = time.gmtime(now() - 86400)
        since = "{:02d}-{}-{}".format(tm.tm_mday, _MON[tm.tm_mon - 1], tm.tm_year)
        typ, data = box.uid("SEARCH", None, "SINCE", since)
        uids = [int(x) for x in (data[0] or b"").split()]
        out, others = [], []
        for u in [x for x in uids if x > last_uid][-80:]:
            typ, md = box.uid("FETCH", str(u), "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            if typ != "OK" or not md or not isinstance(md[0], tuple):
                continue
            hm = email.message_from_bytes(md[0][1])
            try:
                frm = str(make_header(decode_header(hm.get("From", "") or "")))
            except Exception:
                frm = hm.get("From", "") or ""
            dom = _from_domain(frm)
            if not dom_allowed(dom, allowed):
                if dom and dom not in others:
                    others.append(dom)
                continue
            typ, md = box.uid("FETCH", str(u), "(BODY.PEEK[])")
            if typ == "OK" and md and isinstance(md[0], tuple):
                out.append(_parse_email(md[0][1]))
        return (max(uids) if uids else last_uid), out, others
    finally:
        try:
            box.logout()
        except Exception:
            pass


def _recent(entry):
    MAILSTAT["recent"].append(entry)
    del MAILSTAT["recent"][:-8]


async def approve_deposit(bot, did, by):
    """Deposit approve (admin button ya auto). True = ab approve hua."""
    if not await _claim_dep(did, "approved", by):
        return False
    d = await dget(f"deposits/{did}")
    amt = d["amount"]
    await add_bal(d["uid"], amt)
    await bump_stats(deposits=amt)
    await notify(bot, d["uid"], "✅ Deposit approved: <b>{}</b> wallet me add ho gaya.{}".format(
        money(amt), " 🤖 (auto verified)" if by == "auto" else ""))
    feed(bot, "💰 <b>New Deposit</b>\n👤 User: <code>{}</code>\n💵 Amount: {}\n✅ Wallet credited instantly".format(
        mask_id(d["uid"]), money(amt)))
    await pay_referral(bot, d["uid"])
    if by == "auto":
        await tolog(bot, "🤖 Auto-approved deposit <code>{}</code>\n👤 <code>{}</code>\n💰 {}\n🔢 UTR {}".format(
            did[-6:], d["uid"], money(amt), esc(d.get("ref"))))
    return True


async def try_auto_approve(bot, did):
    d = await dget(f"deposits/{did}")
    if not d or d.get("status") != "pending" or d["amount"] > AUTO_MAX:
        return False
    for key, mv in (await dget("mail_pay") or {}).items():
        if mv.get("used") or not mv.get("credit"):
            continue
        if d.get("ref") in (mv.get("utrs") or []) and any(abs(a - d["amount"]) < 0.01 for a in (mv.get("amounts") or [])):
            if await claim_once(f"mail_pay/{key}/used", did):
                return await approve_deposit(bot, did, "auto")
    return False


async def allowed_domains():
    extra = (await cfg()).get("mail_domains") or ""
    return sorted(set(PAY_MAIL_DOMAINS + [x.strip().lower() for x in extra.split(",") if x.strip()]))


async def handle_mail(bot, mm):
    if await dget("mail_pay/" + mm["key"]):
        return
    frm = mm["from"]
    dom = _from_domain(frm)
    low = mm["text"].lower()
    utrs = sorted(set(re.findall(r"(?<!\d)\d{12}(?!\d)", mm["text"])))
    amounts = parse_amounts(mm["text"])
    credit = any(w in low for w in CREDIT_WORDS)
    skip = ""
    if not dom_allowed(dom, await allowed_domains()):
        skip = "sender"
    elif MAIL_REQUIRE_DKIM and not _dkim_ok(mm["auth"], dom):
        skip = "dkim fail"
    elif not utrs:
        skip = "no UTR"
    MAILSTAT["seen"] += 1
    _recent({"t": now(), "dom": dom, "amounts": amounts[:4], "utrs": utrs[:2], "credit": credit, "skip": skip})
    if skip:
        LOG.warning("Mail skipped (%s): %s", skip, frm[:50])
        return
    await dset("mail_pay/" + mm["key"], {"t": mm["ts"], "credit": credit, "utrs": utrs, "amounts": amounts})
    for u in utrs:
        did = await dget("utrs/" + u)
        if did and did != "pending":
            await try_auto_approve(bot, did)


async def prune_mail():
    for k, v in (await dget("mail_pay") or {}).items():
        if now() - int(v.get("t", 0)) > 3 * 86400:
            await ddel("mail_pay/" + k)


MAILSTATE = {"last_uid": 0}


async def mail_loop(bot):
    n = 0
    LOG.info("Auto-pay (Gmail) ON: %s", GMAIL_USER)
    while True:
        try:
            allowed = await allowed_domains()
            last, mails, others = await _run(lambda: _imap_new(MAILSTATE["last_uid"], allowed))
            MAILSTATE["last_uid"] = last
            MAILSTAT["last"] = now()
            MAILSTAT["err"] = ""
            for d in others:
                if d not in MAILSTAT["others"]:
                    MAILSTAT["others"].append(d)
            del MAILSTAT["others"][:-20]
            MAILSTAT["others"][:] = [d for d in MAILSTAT["others"] if not dom_allowed(d, allowed)]
            for mm in mails:
                await handle_mail(bot, mm)
            n += 1
            if n % 100 == 1:
                await prune_mail()
        except Exception as e:
            MAILSTAT["err"] = "{}: {}".format(type(e).__name__, str(e)[:100])
            LOG.warning("mail_loop: %s", MAILSTAT["err"])
            await asyncio.sleep(30)
        await asyncio.sleep(MAIL_POLL_SEC)


# ==================== GATE MIDDLEWARE ====================
_last = {}
_maint = {"v": False, "t": 0.0}


async def maintenance_on():
    if time.monotonic() - _maint["t"] > 20:
        _maint["v"] = bool(await dget("settings/maintenance"))
        _maint["t"] = time.monotonic()
    return _maint["v"]


async def _deny(event, text):
    if event.callback_query:
        await event.callback_query.answer(text, show_alert=True)
    elif event.message:
        await event.message.answer(text)


class Gate(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not user or user.is_bot:
            return await handler(event, data)
        await load_admins()
        is_staff = bool(role_of(user.id))
        n = time.monotonic()
        if n - _last.get(user.id, 0) < 0.5:
            if event.callback_query:
                await event.callback_query.answer()
            return
        if len(_last) > 50000:
            _last.clear()
        _last[user.id] = n
        u = await ensure_user(user)
        is_new = u.pop("_new", False)
        data["u"] = u
        if is_new and not is_staff:
            feed(data.get("bot"), "👋 <b>Welcome {}!</b>\n🆔 User ID: <code>{}</code>\n\n🎉 Naya member join hua <b>{}</b> me.".format(
                mask_name(user.full_name), mask_id(user.id), esc(BOT_NAME)))
        if u.get("banned") and not is_staff:
            return await _deny(event, "🚫 Aap banned ho.")
        if not is_staff and await maintenance_on():
            return await _deny(event, "🛠 Maintenance chal raha hai, thodi der baad aao.")
        return await handler(event, data)


# ==================== USER HANDLERS ====================
user_r = Router()


class Dep(StatesGroup):
    amount = State()
    utr = State()
    shot = State()


class PromoS(StatesGroup):
    code = State()


class TkNew(StatesGroup):
    text = State()


class TkUser(StatesGroup):
    text = State()


async def not_joined(bot, uid):
    missing = []
    for ch in await force_list():
        try:
            m = await bot.get_chat_member(ch["id"], uid)
            if m.status in ("left", "kicked"):
                missing.append(ch)
        except Exception:
            pass
    return missing


def join_kb(missing):
    rows = [[B(text="📢 Join " + (c.get("title") or "Channel")[:28], url=c["link"])] for c in missing if c.get("link")]
    rows.append([B(text="✅ I Joined", callback_data="joined")])
    return kb(rows)


async def welcome_text(lang, name=""):
    custom = (await cfg()).get("welcome")
    if custom:
        return custom.replace("{user}", esc(name)).replace("{bot}", esc(BOT_NAME))
    return t(lang, "welcome").format(name=esc(BOT_NAME))


async def pay_referral(bot, uid):
    """Referrer ko fixed REF_BONUS (sirf ek baar per referred user)."""
    if REF_BONUS <= 0:
        return
    ref = await dget(f"users/{uid}/referredBy")
    if not ref or not await claim_once(f"users/{uid}/refPaid", True):
        return
    await add_bal(ref, REF_BONUS)
    await dincr(f"users/{ref}/refEarned", REF_BONUS)
    await notify(bot, ref, "💸 Referral bonus mila: <b>{}</b>".format(money(REF_BONUS)))


def order_text(oid, o):
    left = max(0, o.get("otpExpiry", 0) - now()) // 60
    s = ("📦 <b>Order</b> <code>{}</code>\n🌍 {}\n📞 Number: <code>{}</code>\n💰 {}\n".format(
        oid[-6:], esc(o["country"]), esc(o["phone"]), money(o["price"])))
    if o.get("desc"):
        s += "📝 {}\n".format(esc(o["desc"]))
    s += "Status: {}".format(STATUS.get(o["status"], o["status"]))
    if o["status"] == "waiting_otp":
        s += ("\n⏱ Time left: {} min\n\n1️⃣ Telegram me isi number se login karo\n"
              "2️⃣ Code request karne ke baad 📩 Get / Resend OTP dabao\n"
              "🔄 Code expire ho jaye to Telegram me <b>Resend code</b> dabao, phir dobara OTP button dabao\n"
              "3️⃣ Login ho jaye to ✅ Login Done dabao".format(left))
    return s


def order_kb(oid, o):
    if o["status"] != "waiting_otp":
        return back("hi", "menu:orders")
    rows = [[B(text="📩 Get / Resend OTP", callback_data="otp:" + oid)]]
    if not o.get("otpDelivered") and not o.get("replaced"):
        rows.append([B(text="🔄 Replace Account", callback_data="rep:" + oid)])
    rows.append([B(text="✅ Login Done", callback_data="done:" + oid)])
    return kb(rows)


# ---------- start / menu / language ----------
@user_r.message(CommandStart())
async def start(m: Message, bot: Bot, state: FSMContext, command: CommandObject, u: dict):
    await state.clear()
    arg = command.args or ""
    if arg.startswith("ref_") and arg[4:].isdigit():
        rid = int(arg[4:])
        if rid != m.from_user.id and now() - int(u.get("joinedAt", 0)) < 300 and not u.get("referredBy"):
            if await dget(f"users/{rid}"):
                if await claim_once(f"users/{m.from_user.id}/referredBy", rid):
                    await dincr(f"users/{rid}/refs", 1)
                    await notify(bot, rid, "🎉 Aapke referral link se ek naya user join hua!")
                    if REF_ON == "join":
                        await pay_referral(bot, m.from_user.id)
    lang = L(u)
    miss = await not_joined(bot, m.from_user.id)
    if miss:
        return await m.answer(t(lang, "join"), reply_markup=join_kb(miss))
    await m.answer(await welcome_text(lang, m.from_user.first_name), reply_markup=main_menu(lang, m.from_user.id))


@user_r.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext, u: dict):
    await state.clear()
    await drop_login(m.from_user.id)
    await m.answer("❌ Cancelled.", reply_markup=main_menu(L(u), m.from_user.id))


@user_r.callback_query(F.data == "joined")
async def joined(cb: CallbackQuery, bot: Bot, u: dict):
    if await not_joined(bot, cb.from_user.id):
        return await cb.answer("❌ Abhi sab channels join nahi kiye.", show_alert=True)
    await show(cb, await welcome_text(L(u), cb.from_user.first_name), main_menu(L(u), cb.from_user.id))
    await cb.answer()


@user_r.callback_query(F.data == "menu:main")
async def menu_main(cb: CallbackQuery, state: FSMContext, u: dict):
    await state.clear()
    await show(cb, await welcome_text(L(u), cb.from_user.first_name), main_menu(L(u), cb.from_user.id))
    await cb.answer()


@user_r.callback_query(F.data == "lang:menu")
async def lang_menu(cb: CallbackQuery, u: dict):
    rows = [[B(text="🇮🇳 Hinglish", callback_data="lang:set:hi"), B(text="🇬🇧 English", callback_data="lang:set:en")],
            [B(text=t(L(u), "b_back"), callback_data="menu:main")]]
    await show(cb, "🌐 Language choose karo / Choose language", kb(rows))
    await cb.answer()


@user_r.callback_query(F.data.startswith("lang:set:"))
async def lang_set(cb: CallbackQuery):
    code = cb.data.split(":")[2]
    if code not in TX:
        return await cb.answer()
    await dupd(f"users/{cb.from_user.id}", {"lang": code})
    await show(cb, await welcome_text(code, cb.from_user.first_name), main_menu(code, cb.from_user.id))
    await cb.answer("✅")


# ---------- profile / wallet ----------
@user_r.callback_query(F.data == "menu:profile")
async def profile(cb: CallbackQuery, u: dict):
    d = await dget(f"users/{cb.from_user.id}") or {}
    vname, vpct = vip_info(d.get("spent", 0))
    s = ("👤 <b>Profile</b>\n\n🆔 <code>{}</code>\n💰 Balance: <b>{}</b>\n🛍 Orders: {}\n💸 Total spent: {}\n"
         "🏅 Level: <b>{}</b> ({}% discount)\n👥 Referrals: {}\n📅 Joined: {}"
         .format(cb.from_user.id, money(d.get("balance", 0)), d.get("orders", 0), money(d.get("spent", 0)),
                 vname, vpct, int(d.get("refs", 0) or 0), time.strftime("%d %b %Y", time.gmtime(d.get("joinedAt", 0)))))
    await show(cb, s, back(L(u)))
    await cb.answer()


@user_r.callback_query(F.data == "menu:wallet")
async def wallet(cb: CallbackQuery, u: dict):
    bal = await dget(f"users/{cb.from_user.id}/balance") or 0
    s = "💰 <b>Wallet</b>\n\nBalance: <b>{}</b>".format(money(bal))
    rows = [[B(text="➕ Add Money", callback_data="dep:start")], [B(text=t(L(u), "b_back"), callback_data="menu:main")]]
    await show(cb, s, kb(rows))
    await cb.answer()


# ---------- deposit (UTR + screenshot) ----------
@user_r.callback_query(F.data == "dep:start")
async def dep_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if await not_joined(bot, cb.from_user.id):
        return await cb.answer("Pehle channels join karo, /start dabao.", show_alert=True)
    if not (await upi_info())[0]:
        return await cb.answer("Deposit abhi available nahi hai.", show_alert=True)
    await state.set_state(Dep.amount)
    await show(cb, "💳 <b>Add Money</b>\n\nAmount bhejo (min {}).\n/cancel to abort".format(money(MIN_DEPOSIT)))
    await cb.answer()


@user_r.message(Dep.amount, F.text)
async def dep_amount(m: Message, state: FSMContext):
    try:
        amt = round(float(m.text.replace(",", "").strip()), 2)
    except ValueError:
        return await m.answer("❌ Sahi amount number me bhejo.")
    if amt < MIN_DEPOSIT or amt > 100000:
        return await m.answer("❌ Amount {} se ₹1,00,000 ke beech ho.".format(money(MIN_DEPOSIT)))
    upi, upi_name = await upi_info()
    await state.update_data(amount=amt)
    await state.set_state(Dep.utr)
    link = "upi://pay?pa={}&pn={}&am={:.2f}&cu=INR&tn=Wallet".format(quote(upi, safe="@"), quote(upi_name), amt)
    buf = io.BytesIO()
    qrcode.make(link).save(buf, format="PNG")
    await m.answer_photo(
        BufferedInputFile(buf.getvalue(), "qr.png"),
        caption=("💳 Pay <b>{}</b>\nUPI: <code>{}</code>\n\nQR scan karke exact amount pay karo.\n"
                 "Phir <b>1️⃣ 12 digit UTR</b> aur <b>2️⃣ payment ka screenshot</b> bhejna hoga.\n\n"
                 "👉 Pehle UTR bhejo:\n/cancel to abort").format(money(amt), esc(upi)))


@user_r.message(Dep.utr, F.text)
async def dep_utr(m: Message, state: FSMContext):
    utr = m.text.strip()
    if not re.fullmatch(r"\d{12}", utr):
        return await m.answer("❌ UTR 12 digit ka hona chahiye. Dobara bhejo ya /cancel.")
    if await dget("utrs/" + utr):
        return await m.answer("❌ Yeh UTR pehle use ho chuka hai.")
    await state.update_data(utr=utr)
    await state.set_state(Dep.shot)
    await m.answer("✅ UTR mil gaya.\n\n📸 Ab <b>payment ka screenshot</b> (photo) bhejo.\n/cancel to abort")


@user_r.message(Dep.shot, F.photo)
async def dep_shot(m: Message, state: FSMContext, bot: Bot, u: dict):
    data = await state.get_data()
    amt, utr = data.get("amount"), data.get("utr")
    if not amt or not utr:
        await state.clear()
        return await m.answer("Session expire ho gaya, dobara try karo.", reply_markup=main_menu(L(u), m.from_user.id))
    if not await claim_once("utrs/" + utr, "pending"):
        await state.clear()
        return await m.answer("❌ Yeh UTR pehle use ho chuka hai.", reply_markup=main_menu(L(u), m.from_user.id))
    fid = m.photo[-1].file_id
    did = await dpush("deposits", {"uid": m.from_user.id, "amount": amt, "method": "upi", "ref": utr,
                                   "shot": fid, "status": "pending", "createdAt": now()})
    await dset("utrs/" + utr, did)
    await state.clear()
    await m.answer("✅ Request submit ho gayi. {}".format(
        "Payment auto-verify ho rahi hai, 1-2 min me balance add ho jayega." if MAIL_ON else "Verify hote hi balance add ho jayega."),
        reply_markup=main_menu(L(u), m.from_user.id))
    cap = "💳 <b>New Deposit</b>\n👤 {} (<code>{}</code>)\n💰 {}\n🔢 UTR: <code>{}</code>".format(
        esc(m.from_user.full_name), m.from_user.id, money(amt), esc(utr))
    auto = MAIL_ON and await try_auto_approve(bot, did)
    k = None if auto else kb([[B(text="✅ Approve", callback_data="dap:" + did), B(text="❌ Reject", callback_data="drj:" + did)]])
    if auto:
        cap += "\n\n🤖 <b>Auto-approved</b> (mail se verify)"
    for a in await staff_ids("manager"):
        try:
            await bot.send_photo(a, fid, caption=cap, reply_markup=k)
        except Exception as e:
            LOG.warning("deposit notify failed %s: %s", a, e)


@user_r.message(Dep.shot)
async def dep_shot_wrong(m: Message):
    await m.answer("❌ Screenshot <b>photo</b> ke roop me bhejo (file nahi). /cancel to abort")


# ---------- buy ----------
@user_r.callback_query(F.data == "menu:buy")
async def buy_menu(cb: CallbackQuery, bot: Bot, u: dict):
    if await not_joined(bot, cb.from_user.id):
        return await cb.answer("Pehle channels join karo, /start dabao.", show_alert=True)
    avail = await dget("available") or {}
    countries = await dget("countries") or {}
    rows = []
    for ck, items in avail.items():
        n = len(items or {})
        if n == 0 or ck not in countries:
            continue
        c = countries[ck]
        rows.append([B(text="{} • {} • {} left".format(c.get("name", ck), money(c.get("price", 0)), n), callback_data="buy:" + ck)])
    rows = rows[:90]
    rows.append([B(text=t(L(u), "b_back"), callback_data="menu:main")])
    s = "🛒 <b>Select Country</b>" if len(rows) > 1 else "😔 Abhi stock available nahi hai. Thodi der baad try karo."
    gd = await global_discount()
    if gd and len(rows) > 1:
        s += "\n🔥 Sale live: extra {}% off".format(int(gd))
    await show(cb, s, kb(rows))
    await cb.answer()


@user_r.callback_query(F.data.startswith("buy:"))
async def buy_qty(cb: CallbackQuery, u: dict):
    ck = cb.data[4:]
    c = await dget(f"countries/{ck}")
    n = len(await dget(f"available/{ck}") or {})
    if not c or n == 0:
        return await cb.answer("❌ Out of stock.", show_alert=True)
    d = await dget(f"users/{cb.from_user.id}") or {}
    unit, pct = await calc_price(c["price"], 1, d.get("spent", 0))
    s = "🌍 <b>{}</b>\n💰 Price: <b>{}</b>{}\n📦 Stock: {}".format(
        esc(c["name"]), money(unit), " (-{}%)".format(int(pct)) if pct else "", n)
    if BULK_TABLE:
        s += "\n🎁 Bulk: " + ", ".join("{}+ → {}% off".format(int(a), int(p)) for a, p in BULK_TABLE)
    s += "\n\nKitne accounts chahiye?"
    qtys = [q for q in (1, 2, 3, 5, 10) if q <= n]
    rows = [[B(text="{}x".format(q), callback_data="bq:{}:{}".format(ck, q)) for q in qtys]]
    rows.append([B(text=t(L(u), "b_back"), callback_data="menu:buy")])
    await show(cb, s, kb(rows))
    await cb.answer()


@user_r.callback_query(F.data.startswith("bq:"))
async def buy_confirm_screen(cb: CallbackQuery, u: dict):
    _, ck, q = cb.data.split(":")
    q = int(q)
    c = await dget(f"countries/{ck}")
    n = len(await dget(f"available/{ck}") or {})
    if not c or n < q:
        return await cb.answer("❌ Itna stock nahi hai.", show_alert=True)
    d = await dget(f"users/{cb.from_user.id}") or {}
    unit, pct = await calc_price(c["price"], q, d.get("spent", 0))
    total = round(unit * q, 2)
    bal = d.get("balance", 0)
    s = "🌍 <b>{}</b>\n🔢 Qty: {}\n💰 Unit: {}{}\n🧾 Total: <b>{}</b>\n👛 Balance: {}".format(
        esc(c["name"]), q, money(unit), " (-{}%)".format(int(pct)) if pct else "", money(total), money(bal))
    if bal >= total:
        rows = [[B(text="✅ Pay {}".format(money(total)), callback_data="bc:{}:{}".format(ck, q))]]
    else:
        rows = [[B(text="➕ Add Money (low balance)", callback_data="dep:start")]]
    rows.append([B(text=t(L(u), "b_back"), callback_data="buy:" + ck)])
    await show(cb, s, kb(rows))
    await cb.answer()


@user_r.callback_query(F.data.startswith("bc:"))
async def buy_confirm(cb: CallbackQuery, bot: Bot, u: dict):
    _, ck, q = cb.data.split(":")
    q = int(q)
    uid = cb.from_user.id
    c = await dget(f"countries/{ck}")
    if not c or q < 1 or q > 10:
        return await cb.answer("❌ Not available.", show_alert=True)
    spent = await dget(f"users/{uid}/spent") or 0
    unit, _ = await calc_price(c["price"], q, spent)
    total = round(unit * q, 2)
    if not await ded_bal(uid, total):
        return await cb.answer("❌ Balance kam hai.", show_alert=True)
    got = []
    for _i in range(q):
        sid = await lock_stock(ck, uid)
        if not sid:
            break
        got.append(sid)
    if len(got) < q:
        await add_bal(uid, round(unit * (q - len(got)), 2))
    if not got:
        return await cb.answer("❌ Stock khatam ho gaya, paise wapas.", show_alert=True)
    await show(cb, "✅ {} account(s) ready 👇".format(len(got)), back(L(u)))
    masked = []
    for sid in got:
        st = await dget(f"stock/{ck}/{sid}")
        oid, o = await create_order(uid, ck, c, sid, st["phone"], unit, st.get("desc", ""))
        masked.append(mask_phone(st["phone"]))
        await cb.message.answer(order_text(oid, o), reply_markup=order_kb(oid, o))
        await tolog(bot, "🛒 New order\n🧾 <code>{}</code>\n👤 <code>{}</code>\n🌍 {}\n📞 {}\n💰 {}".format(
            oid, uid, esc(c["name"]), mask_phone(st["phone"]), money(unit)))
    feed(bot, ("🛒 <b>New Purchase</b>\n👤 User: <code>{}</code>\n🌍 {}\n📞 {}\n🔢 Qty: {}\n💵 Paid: {}\n"
               "⚡ Delivered instantly").format(mask_id(uid), esc(c["name"]), esc(", ".join(masked)), len(got), money(unit * len(got))))
    left = len(await dget(f"available/{ck}") or {})
    if left <= LOW_STOCK and (left == LOW_STOCK or left == 0):
        for a in await staff_ids("manager"):
            await notify(bot, a, "⚠️ Low stock: <b>{}</b> — {} left".format(esc(c["name"]), left))
    await cb.answer("✅ Order placed")


# ---------- orders / otp / replace / done ----------
@user_r.callback_query(F.data == "menu:orders")
async def my_orders(cb: CallbackQuery, u: dict):
    ids = list((await dget(f"user_orders/{cb.from_user.id}") or {}).keys())[-10:][::-1]
    rows = []
    for oid in ids:
        o = await dget(f"orders/{oid}")
        if o:
            rows.append([B(text="{} • {}".format(o["country"], STATUS.get(o["status"], o["status"])), callback_data="ord:" + oid)])
    rows.append([B(text=t(L(u), "b_back"), callback_data="menu:main")])
    await show(cb, "📦 <b>My Orders</b>" if len(rows) > 1 else "📦 Abhi koi order nahi hai.", kb(rows))
    await cb.answer()


@user_r.callback_query(F.data.startswith("ord:"))
async def order_view(cb: CallbackQuery):
    oid = cb.data[4:]
    o = await dget(f"orders/{oid}")
    if not o or o["uid"] != cb.from_user.id:
        return await cb.answer("❌ Order not found.", show_alert=True)
    await show(cb, order_text(oid, o), order_kb(oid, o))
    await cb.answer()


@user_r.callback_query(F.data.startswith("otp:"))
async def get_otp(cb: CallbackQuery, bot: Bot):
    oid = cb.data[4:]
    o = await dget(f"orders/{oid}")
    if not o or o["uid"] != cb.from_user.id:
        return await cb.answer("❌ Order not found.", show_alert=True)
    if o["status"] != "waiting_otp":
        return await cb.answer("Order closed ho chuka hai.", show_alert=True)
    if now() > o["otpExpiry"]:
        return await cb.answer("⏱ OTP window expire ho gayi.", show_alert=True)
    if o.get("otpCount", 0) >= 40:
        return await cb.answer("Limit reach ho gayi. Support se contact karo.", show_alert=True)
    await cb.answer("⏳ Checking...")
    await dincr(f"orders/{oid}/otpCount", 1)
    st = await dget(f"stock/{o['ckey']}/{o['sid']}")
    if not st or not st.get("sess"):
        return await cb.message.answer("❌ Account data missing. Support se contact karo.")
    res = await fetch_code(dec(st["sess"]), o["createdAt"])
    if res["status"] == "ok":
        if o.get("lastMsgId") == res["id"]:
            return await cb.message.answer(
                "⌛ Naya code abhi nahi aaya.\nPehla code expire ho gaya ho to Telegram me <b>Resend code</b> dabao, "
                "phir dobara 📩 Get / Resend OTP dabao.\n\n(Last code: <code>{}</code>)".format(res["code"]))
        await dupd(f"orders/{oid}", {"otpDelivered": True, "lastCode": res["code"], "lastMsgId": res["id"]})
        o["otpDelivered"] = True
        s = "📩 <b>Login Code:</b> <code>{}</code>".format(res["code"])
        age = now() - res["date"]
        if age > 150:
            s += "\n⚠️ Ye code {} min purana hai, expire ho gaya ho to Resend code karke dobara dabao.".format(age // 60)
        if st.get("tfa"):
            s += "\n🔑 2FA Password: <code>{}</code>".format(esc(dec(st["tfa"])))
        s += "\n\nLogin hone ke baad ✅ Login Done dabao."
        await cb.message.answer(s, reply_markup=order_kb(oid, o))
    elif res["status"] == "none":
        await cb.message.answer("⌛ Abhi code nahi aaya. Telegram me number daalke code request karo, phir 📩 Get / Resend OTP dabao.")
    elif res["status"] == "dead":
        await refund_order(bot, oid, "Account me problem aayi, auto refund.", dead=True)
    else:
        await cb.message.answer("⚠️ Temporary error, kuch second baad dobara try karo.")


@user_r.callback_query(F.data.startswith("rep:"))
async def replace_cb(cb: CallbackQuery, bot: Bot):
    oid = cb.data[4:]
    res, o = await replace_order(bot, oid, cb.from_user.id)
    if res == "no":
        return await cb.answer("Replacement available nahi hai (OTP aa chuka ya pehle replace ho chuka).", show_alert=True)
    if res == "refund":
        await show(cb, "↩️ Stock nahi mila, paise wallet me wapas aa gaye.", back("hi"))
        return await cb.answer()
    await show(cb, order_text(oid, o), order_kb(oid, o))
    await cb.answer("🔄 Account replaced")


@user_r.callback_query(F.data.startswith("done:"))
async def login_done(cb: CallbackQuery, bot: Bot, u: dict):
    oid = cb.data[5:]
    o = await dget(f"orders/{oid}")
    if not o or o["uid"] != cb.from_user.id:
        return await cb.answer("❌ Order not found.", show_alert=True)
    if o["status"] != "waiting_otp":
        return await cb.answer("Order already closed.", show_alert=True)
    if not o.get("otpDelivered"):
        return await cb.answer("Pehle 📩 Get OTP se code lo.", show_alert=True)
    await cb.answer("⏳ Finalizing...")
    await complete_order(bot, oid)
    await show(cb, "✅ <b>Order completed!</b>", back(L(u)))


# ---------- referral / promo / leaderboard ----------
@user_r.callback_query(F.data == "menu:ref")
async def referral(cb: CallbackQuery, u: dict):
    d = await dget(f"users/{cb.from_user.id}") or {}
    link = "https://t.me/{}?start=ref_{}".format(BOT_USER["n"], cb.from_user.id)
    when = "user ke join karte hi" if REF_ON == "join" else "jab wo apna pehla deposit kare"
    s = ("👥 <b>Referral</b>\n\nHar referral par aapko <b>{}</b> milta hai ({}).\n\n"
         "🔗 Your link:\n<code>{}</code>\n\n👥 Referrals: {}\n💰 Earned: {}".format(
             money(REF_BONUS), when, link, int(d.get("refs", 0) or 0), money(d.get("refEarned", 0))))
    await show(cb, s, back(L(u)))
    await cb.answer()


@user_r.callback_query(F.data == "menu:promo")
async def promo_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PromoS.code)
    await show(cb, "🏷 Promo code bhejo.\n/cancel to abort")
    await cb.answer()


@user_r.message(PromoS.code, F.text)
async def promo_apply(m: Message, state: FSMContext, u: dict):
    code = re.sub(r"[^A-Z0-9]", "", m.text.upper())
    p = await dget(f"promos/{code}") if code else None
    if not p:
        return await m.answer("❌ Invalid code. Dobara bhejo ya /cancel.")
    await state.clear()
    uid = m.from_user.id
    if not await claim_once(f"promos/{code}/users/{uid}", True):
        return await m.answer("❌ Aap yeh code pehle use kar chuke ho.", reply_markup=main_menu(L(u), uid))
    box = {"ok": False}
    mx = int(p.get("max", 0) or 0)

    def f(cur):
        cur = cur or 0
        if mx and cur >= mx:
            box["ok"] = False
            return cur
        box["ok"] = True
        return cur + 1

    await dtx(f"promos/{code}/used", f)
    if not box["ok"]:
        await ddel(f"promos/{code}/users/{uid}")
        return await m.answer("❌ Code ki limit khatam ho chuki hai.", reply_markup=main_menu(L(u), uid))
    await add_bal(uid, p["amount"])
    await m.answer("✅ {} wallet me add hua!".format(money(p["amount"])), reply_markup=main_menu(L(u), uid))


@user_r.callback_query(F.data == "menu:top")
async def top_menu(cb: CallbackQuery, u: dict):
    rows = [[B(text="💸 Top Buyers", callback_data="top:spent"), B(text="👥 Top Referrers", callback_data="top:refs")],
            [B(text=t(L(u), "b_back"), callback_data="menu:main")]]
    await show(cb, "🏆 <b>Leaderboard</b>", kb(rows))
    await cb.answer()


@user_r.callback_query(F.data.in_({"top:spent", "top:refs"}))
async def top_view(cb: CallbackQuery):
    key = cb.data[4:]
    us = await dget("users") or {}
    ranked = sorted(us.values(), key=lambda v: float(v.get(key, 0) or 0), reverse=True)[:10]
    lines = []
    for i, v in enumerate(ranked, 1):
        val = float(v.get(key, 0) or 0)
        if val <= 0:
            break
        nm = esc((v.get("name") or "User")[:2]) + "***"
        lines.append("{}. {} — {}".format(i, nm, money(val) if key == "spent" else int(val)))
    s = "🏆 <b>{}</b>\n\n".format("Top Buyers" if key == "spent" else "Top Referrers") + ("\n".join(lines) if lines else "Abhi koi data nahi.")
    await show(cb, s, back("hi", "menu:top"))
    await cb.answer()


# ---------- support tickets (user side) ----------
def ticket_staff_kb(tid):
    return kb([[B(text="💬 Reply", callback_data="tkre:" + tid), B(text="✅ Close", callback_data="tkcl:" + tid)]])


@user_r.callback_query(F.data == "sup:menu")
async def sup_menu(cb: CallbackQuery, u: dict):
    rows = [[B(text="🎫 New Ticket", callback_data="sup:new")]]
    if SUPPORT_USERNAME:
        rows.append([B(text="💬 Chat with Support", url="https://t.me/" + SUPPORT_USERNAME)])
    rows.append([B(text=t(L(u), "b_back"), callback_data="menu:main")])
    await show(cb, "🆘 <b>Support</b>\n\nTicket banao, team jaldi reply karegi.", kb(rows))
    await cb.answer()


@user_r.callback_query(F.data == "sup:new")
async def ticket_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TkNew.text)
    await show(cb, "🎫 Apni problem likh kar bhejo.\n/cancel to abort")
    await cb.answer()


@user_r.message(TkNew.text, F.text)
async def ticket_create(m: Message, state: FSMContext, bot: Bot, u: dict):
    await state.clear()
    text = m.text[:1500]
    tid = await dpush("tickets", {"uid": m.from_user.id, "name": m.from_user.full_name, "text": text,
                                  "status": "open", "createdAt": now()})
    await m.answer("✅ Ticket <code>#{}</code> create ho gaya. Reply yahin milega.".format(tid[-6:]),
                   reply_markup=main_menu(L(u), m.from_user.id))
    s = "🎫 <b>New Ticket</b> <code>#{}</code>\n👤 {} (<code>{}</code>)\n\n{}".format(tid[-6:], esc(m.from_user.full_name), m.from_user.id, esc(text))
    for a in await staff_ids("support"):
        await notify(bot, a, s, ticket_staff_kb(tid))


@user_r.callback_query(F.data.startswith("tkr:"))
async def ticket_user_reply(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TkUser.text)
    await state.update_data(tid=cb.data[4:])
    await cb.message.answer("💬 Apna reply likho.\n/cancel to abort")
    await cb.answer()


@user_r.message(TkUser.text, F.text)
async def ticket_user_send(m: Message, state: FSMContext, bot: Bot, u: dict):
    tid = (await state.get_data()).get("tid")
    await state.clear()
    tk = await dget(f"tickets/{tid}") if tid else None
    if not tk or tk.get("uid") != m.from_user.id:
        return await m.answer("❌ Ticket nahi mila.", reply_markup=main_menu(L(u), m.from_user.id))
    await dupd(f"tickets/{tid}", {"status": "open", "last": m.text[:1500]})
    await m.answer("✅ Reply bhej diya.", reply_markup=main_menu(L(u), m.from_user.id))
    s = "🎫 <b>Ticket reply</b> <code>#{}</code>\n👤 {} (<code>{}</code>)\n\n{}".format(tid[-6:], esc(m.from_user.full_name), m.from_user.id, esc(m.text[:1500]))
    for a in await staff_ids("support"):
        await notify(bot, a, s, ticket_staff_kb(tid))


# ==================== STAFF / ADMIN HANDLERS ====================
staff_r = Router()   # support+ : panel + tickets
mgr_r = Router()     # manager+ : stock, users, deposits, broadcast, promos ...
own_r = Router()     # owner    : admins, backup
staff_r.message.filter(Staff("support"))
staff_r.callback_query.filter(Staff("support"))
mgr_r.message.filter(Staff("manager"))
mgr_r.callback_query.filter(Staff("manager"))
own_r.message.filter(Staff("owner"))
own_r.callback_query.filter(Staff("owner"))


class StaffReply(StatesGroup):
    text = State()


class AddStock(StatesGroup):
    lines = State()


class SetPrice(StatesGroup):
    price = State()


class AdmUser(StatesGroup):
    uid = State()


class AdmAmt(StatesGroup):
    amount = State()


class AdmOrd(StatesGroup):
    oid = State()


class AdmBC(StatesGroup):
    msg = State()


class AdmPromo(StatesGroup):
    data = State()


class AdmDisc(StatesGroup):
    data = State()


class AdmAdmins(StatesGroup):
    data = State()


class AddAcc(StatesGroup):
    info = State()
    phone = State()
    code = State()
    pwd = State()
    desc = State()


class AdmChan(StatesGroup):
    ref = State()


class AdmUpi(StatesGroup):
    data = State()


class AdmWelcome(StatesGroup):
    text = State()


def to_panel():
    return kb([[B(text="⬅️ Admin Panel", callback_data="adm:main")]])


async def panel_kb(uid):
    role = role_of(uid)
    maint = bool(await dget("settings/maintenance"))
    rows = [[B(text="🎫 Tickets", callback_data="adm:tickets")]]
    if RANK[role] >= 2:
        rows += [
            [B(text="➕ Add Account (Login)", callback_data="adm:login")],
            [B(text="📥 Add Stock (Session)", callback_data="adm:add"), B(text="📦 Stock", callback_data="adm:stock")],
            [B(text="👥 Users", callback_data="adm:user"), B(text="🧾 Orders", callback_data="adm:order")],
            [B(text="📢 Broadcast", callback_data="adm:bc"), B(text="🏷 Promo Code", callback_data="adm:promo")],
            [B(text="🔥 Discount", callback_data="adm:disc"), B(text="📊 Stats", callback_data="adm:stats")],
            [B(text="📣 Channels", callback_data="adm:chan"), B(text="🏦 UPI", callback_data="adm:upi")],
            [B(text="✏️ Welcome Msg", callback_data="adm:welcome"), B(text="📬 Auto-Pay", callback_data="adm:mail")],
            [B(text="📤 Export CSV", callback_data="adm:export"),
             B(text="🛠 Maintenance: {}".format("ON" if maint else "OFF"), callback_data="adm:maint")],
        ]
    if RANK[role] >= 3:
        rows.append([B(text="🛡 Admins", callback_data="adm:admins"), B(text="🗄 Backup", callback_data="adm:backup")])
    rows.append([B(text="⬅️ Menu", callback_data="menu:main")])
    return kb(rows)


async def audit(bot, m_or_cb, text):
    u = m_or_cb.from_user
    await tolog(bot, "🛡 <b>{}</b> <code>{}</code>: {}".format(role_of(u.id), u.id, text))


@staff_r.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙️ <b>Admin Panel</b> ({})".format(role_of(m.from_user.id)), reply_markup=await panel_kb(m.from_user.id))


@staff_r.callback_query(F.data == "adm:main")
async def admin_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "⚙️ <b>Admin Panel</b> ({})".format(role_of(cb.from_user.id)), await panel_kb(cb.from_user.id))
    await cb.answer()


# ---------- tickets ----------
@staff_r.callback_query(F.data == "adm:tickets")
async def tickets_list(cb: CallbackQuery):
    tks = await dget("tickets") or {}
    openl = [(k, v) for k, v in tks.items() if v.get("status") == "open"][-15:][::-1]
    rows = [[B(text="#{} • {}".format(k[-6:], (v.get("name") or "")[:18]), callback_data="tka:" + k)] for k, v in openl]
    rows.append([B(text="⬅️ Admin Panel", callback_data="adm:main")])
    await show(cb, "🎫 <b>Open tickets</b>: {}".format(len(openl)), kb(rows))
    await cb.answer()


@staff_r.callback_query(F.data.startswith("tka:"))
async def ticket_view(cb: CallbackQuery):
    tid = cb.data[4:]
    tk = await dget(f"tickets/{tid}")
    if not tk:
        return await cb.answer("Not found.", show_alert=True)
    s = "🎫 <b>#{}</b> ({})\n👤 {} (<code>{}</code>)\n\n{}".format(tid[-6:], tk.get("status"), esc(tk.get("name")), tk["uid"], esc(tk.get("text")))
    if tk.get("last"):
        s += "\n\n↩️ Last user msg:\n" + esc(tk["last"])
    await show(cb, s, ticket_staff_kb(tid))
    await cb.answer()


@staff_r.callback_query(F.data.startswith("tkre:"))
async def ticket_reply_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(StaffReply.text)
    await state.update_data(tid=cb.data[5:])
    await cb.message.answer("💬 Reply likho (user ko bhej diya jayega).\n/cancel to abort")
    await cb.answer()


@staff_r.message(StaffReply.text, F.text)
async def ticket_reply_send(m: Message, state: FSMContext, bot: Bot):
    tid = (await state.get_data()).get("tid")
    await state.clear()
    tk = await dget(f"tickets/{tid}") if tid else None
    if not tk:
        return await m.answer("❌ Ticket nahi mila.")
    await dupd(f"tickets/{tid}", {"lastReplyBy": m.from_user.id})
    ok = await notify(bot, tk["uid"], "🎫 <b>Ticket #{} — Support reply</b>\n\n{}".format(tid[-6:], esc(m.text)),
                      kb([[B(text="💬 Reply", callback_data="tkr:" + tid)]]))
    await m.answer("✅ Reply bhej diya." if ok else "⚠️ User ko message nahi ja paya.", reply_markup=to_panel())


@staff_r.callback_query(F.data.startswith("tkcl:"))
async def ticket_close(cb: CallbackQuery, bot: Bot):
    tid = cb.data[5:]
    tk = await dget(f"tickets/{tid}")
    if not tk:
        return await cb.answer("Not found.", show_alert=True)
    await dupd(f"tickets/{tid}", {"status": "closed"})
    await notify(bot, tk["uid"], "✅ Ticket <code>#{}</code> close ho gaya.".format(tid[-6:]))
    await cb.answer("Closed")


# ---------- add stock ----------
@mgr_r.callback_query(F.data == "adm:add")
async def add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddStock.lines)
    await show(cb, (
        "➕ <b>Add Stock</b>\n\nHar line me ek account:\n"
        "<code>country|price|session_string|2fa(optional)|description(optional)</code>\n\n"
        "Example:\n<code>🇮🇳 India|45|1BVtsOKw...|mypass|Fresh account, 2024</code>\n"
        "2FA nahi hai to khali chhodo: <code>🇮🇳 India|45|1BVtsOKw...||Fresh</code>\n\n"
        "• Country koi bhi likh sakte ho (flag emoji ke saath bhi)\n"
        "• Price us country ka update ho jata hai\n"
        "• Phone number session se auto detect hota hai\n"
        "• Bahut accounts ho to <b>.txt file</b> bhejo\n/cancel to abort"), to_panel())
    await cb.answer()


@mgr_r.message(AddStock.lines, F.text | F.document)
async def add_lines(m: Message, bot: Bot, state: FSMContext):
    if m.document:
        buf = io.BytesIO()
        await bot.download(m.document, destination=buf)
        text = buf.getvalue().decode("utf-8", "ignore")
    else:
        text = m.text or ""
    lines = [x.strip() for x in text.splitlines() if x.strip() and not x.startswith("#")]
    await state.clear()
    if not lines:
        return await m.answer("❌ Koi line nahi mili.", reply_markup=to_panel())
    status = await m.answer("⏳ Checking {} account(s)...".format(len(lines)))
    ok, fails = 0, []
    for i, ln in enumerate(lines, 1):
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 3:
            fails.append("#{} wrong format".format(i))
            continue
        country, price_s, sess = parts[:3]
        tfa = parts[3] if len(parts) > 3 else ""
        desc = parts[4] if len(parts) > 4 else ""
        try:
            price = float(price_s)
        except ValueError:
            fails.append("#{} bad price".format(i))
            continue
        phone = await health_check(sess)
        if not phone:
            fails.append("#{} dead/invalid session".format(i))
            continue
        if await add_item(country, price, phone, sess, tfa, desc) == "dup":
            fails.append("#{} {} duplicate".format(i, esc(phone)))
        else:
            ok += 1
        if i % 5 == 0:
            try:
                await status.edit_text("⏳ {}/{} checked...".format(i, len(lines)))
            except Exception:
                pass
    s = "✅ Added: <b>{}</b>\n❌ Failed: <b>{}</b>".format(ok, len(fails))
    if fails:
        s += "\n\n" + "\n".join(fails[:30])
    await status.edit_text(s, reply_markup=to_panel())
    await audit(bot, m, "added stock: {} ok / {} failed".format(ok, len(fails)))


# ---------- stock management ----------
@mgr_r.callback_query(F.data == "adm:stock")
async def stock_view(cb: CallbackQuery):
    avail = await dget("available") or {}
    countries = await dget("countries") or {}
    rows = [[B(text="{} • {} • {}".format(c.get("name", ck), money(c.get("price", 0)), len(avail.get(ck) or {})), callback_data="sc:" + ck)]
            for ck, c in countries.items()][:90]
    rows.append([B(text="⬅️ Admin Panel", callback_data="adm:main")])
    await show(cb, "📦 <b>Stock</b> (country tap karo)" if len(rows) > 1 else "📦 Stock empty.", kb(rows))
    await cb.answer()


@mgr_r.callback_query(F.data.startswith("sc:"))
async def country_card(cb: CallbackQuery):
    ck = cb.data[3:]
    c = await dget(f"countries/{ck}")
    if not c:
        return await cb.answer("Not found.", show_alert=True)
    n = len(await dget(f"available/{ck}") or {})
    rows = [[B(text="💲 Set Price", callback_data="sp:" + ck), B(text="🗑 Delete Available", callback_data="sd:" + ck)],
            [B(text="⬅️ Stock", callback_data="adm:stock")]]
    await show(cb, "🌍 <b>{}</b>\n💰 {}\n📦 Available: {}".format(esc(c["name"]), money(c["price"]), n), kb(rows))
    await cb.answer()


@mgr_r.callback_query(F.data.startswith("sp:"))
async def price_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetPrice.price)
    await state.update_data(ck=cb.data[3:])
    await cb.message.answer("💲 Naya price bhejo.\n/cancel to abort")
    await cb.answer()


@mgr_r.message(SetPrice.price, F.text)
async def price_apply(m: Message, state: FSMContext, bot: Bot):
    try:
        price = float(m.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        return await m.answer("❌ Positive number bhejo.")
    ck = (await state.get_data()).get("ck")
    await state.clear()
    await dupd(f"countries/{ck}", {"price": price})
    await m.answer("✅ Price updated: {}".format(money(price)), reply_markup=to_panel())
    await audit(bot, m, "price {} -> {}".format(ck, money(price)))


@mgr_r.callback_query(F.data.startswith("sd:"))
async def stock_del_ask(cb: CallbackQuery):
    ck = cb.data[3:]
    rows = [[B(text="⚠️ Yes, delete all available", callback_data="sdc:" + ck)], [B(text="⬅️ Cancel", callback_data="sc:" + ck)]]
    await show(cb, "⚠️ Is country ka saara <b>available</b> stock delete hoga. Sure?", kb(rows))
    await cb.answer()


@mgr_r.callback_query(F.data.startswith("sdc:"))
async def stock_del(cb: CallbackQuery, bot: Bot):
    ck = cb.data[4:]
    ids = list((await dget(f"available/{ck}") or {}).keys())
    removed = 0
    for sid in ids:
        box = {"ok": False}

        def f(cur):
            if cur and cur.get("status") == "available":
                box["ok"] = True
                return None
            box["ok"] = False
            return cur

        await dtx(f"stock/{ck}/{sid}", f)
        await ddel(f"available/{ck}/{sid}")
        removed += 1 if box["ok"] else 0
    await show(cb, "🗑 {} account(s) deleted.".format(removed), kb([[B(text="⬅️ Stock", callback_data="adm:stock")]]))
    await audit(bot, cb, "deleted {} stock from {}".format(removed, ck))
    await cb.answer()


# ---------- stats / maintenance / discount ----------
@mgr_r.callback_query(F.data == "adm:stats")
async def stats_view(cb: CallbackQuery):
    s = await dget("stats") or {}
    users = len(await dkeys("users"))
    avail = await dget("available") or {}
    total = sum(len(v or {}) for v in avail.values())
    active = len(await dget("active_orders") or {})
    daily = s.get("daily") or {}
    lines = []
    for dk in sorted(daily.keys())[-7:][::-1]:
        d = daily[dk]
        lines.append("{}: 🛍 {} • 💰 {} • 💳 {}".format(dk, int(d.get("orders", 0) or 0), money(d.get("revenue", 0)), money(d.get("deposits", 0))))
    txt = ("📊 <b>Stats</b>\n\n👥 Users: {}\n🛍 Orders: {}\n💰 Revenue: {}\n💳 Deposits: {}\n"
           "📦 Stock available: {}\n⏳ Active orders: {}\n\n<b>Last 7 days</b>\n{}".format(
               users, int(s.get("orders", 0) or 0), money(s.get("revenue", 0)), money(s.get("deposits", 0)),
               total, active, "\n".join(lines) if lines else "No data"))
    await show(cb, txt, to_panel())
    await cb.answer()


@mgr_r.callback_query(F.data == "adm:maint")
async def maint_toggle(cb: CallbackQuery, bot: Bot):
    cur = bool(await dget("settings/maintenance"))
    await dset("settings/maintenance", not cur)
    _maint["t"] = 0.0
    await show(cb, "⚙️ <b>Admin Panel</b> ({})".format(role_of(cb.from_user.id)), await panel_kb(cb.from_user.id))
    await cb.answer("Maintenance " + ("OFF" if cur else "ON"))
    await audit(bot, cb, "maintenance " + ("OFF" if cur else "ON"))


@mgr_r.callback_query(F.data == "adm:disc")
async def disc_start(cb: CallbackQuery, state: FSMContext):
    cur = await global_discount()
    await state.set_state(AdmDisc.data)
    await show(cb, ("🔥 <b>Global discount / Flash sale</b>\nAbhi: {}%\n\nFormat: <code>percent hours</code>\n"
                    "Example: <code>20 24</code> (20% for 24h)\n<code>20</code> = jab tak band na karo\n"
                    "<code>0</code> = band\n/cancel to abort").format(int(cur)), to_panel())
    await cb.answer()


@mgr_r.message(AdmDisc.data, F.text)
async def disc_apply(m: Message, state: FSMContext, bot: Bot):
    parts = m.text.split()
    try:
        pct = float(parts[0])
        hrs = float(parts[1]) if len(parts) > 1 else 0
        if pct < 0 or pct > 60 or hrs < 0:
            raise ValueError
    except (ValueError, IndexError):
        return await m.answer("❌ Format: <code>percent hours</code> (percent 0-60).")
    await state.clear()
    await dset("settings/discount", {"pct": pct, "until": int(now() + hrs * 3600) if hrs else 0})
    await m.answer("✅ Discount set: {}%{}".format(int(pct), " for {}h".format(hrs) if hrs else ""), reply_markup=to_panel())
    await audit(bot, m, "discount {}% {}h".format(pct, hrs))


# ---------- users ----------
async def ucard(uid):
    d = await dget(f"users/{uid}")
    if not d:
        return None, None
    vname, _ = vip_info(d.get("spent", 0))
    s = ("👤 <b>{}</b> @{}\n🆔 <code>{}</code>\n💰 {} | 🛍 {} orders | 💸 {}\n🏅 {} | 👥 {} refs\n📅 {}\n🚫 Banned: {}".format(
        esc(d.get("name")), esc(d.get("username")), uid, money(d.get("balance", 0)), d.get("orders", 0), money(d.get("spent", 0)),
        vname, int(d.get("refs", 0) or 0), time.strftime("%d %b %Y", time.gmtime(d.get("joinedAt", 0))),
        "Yes" if d.get("banned") else "No"))
    ban = B(text="✅ Unban", callback_data="unb:{}".format(uid)) if d.get("banned") else B(text="🚫 Ban", callback_data="ub:{}".format(uid))
    k = kb([[ban], [B(text="➕ Add Balance", callback_data="ab:{}".format(uid)), B(text="➖ Deduct", callback_data="sb:{}".format(uid))],
            [B(text="⬅️ Admin Panel", callback_data="adm:main")]])
    return s, k


@mgr_r.callback_query(F.data == "adm:user")
async def user_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdmUser.uid)
    await show(cb, "👥 User ka Telegram ID bhejo.\n/cancel to abort", to_panel())
    await cb.answer()


@mgr_r.message(AdmUser.uid, F.text)
async def user_lookup(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        return await m.answer("❌ Numeric ID bhejo.")
    s, k = await ucard(int(m.text.strip()))
    if not s:
        return await m.answer("❌ User nahi mila. Dobara bhejo ya /cancel.")
    await state.clear()
    await m.answer(s, reply_markup=k)


@mgr_r.callback_query(F.data.regexp(r"^(ub|unb):\d+$"))
async def ban_toggle(cb: CallbackQuery, bot: Bot):
    act, uid = cb.data.split(":")
    await dupd(f"users/{uid}", {"banned": act == "ub"})
    s, k = await ucard(int(uid))
    await show(cb, s, k)
    await cb.answer("Done")
    await audit(bot, cb, "{} user {}".format("banned" if act == "ub" else "unbanned", uid))


@mgr_r.callback_query(F.data.regexp(r"^(ab|sb):\d+$"))
async def bal_start(cb: CallbackQuery, state: FSMContext):
    act, uid = cb.data.split(":")
    await state.set_state(AdmAmt.amount)
    await state.update_data(uid=int(uid), mode=act)
    await cb.message.answer("💰 Amount bhejo.\n/cancel to abort")
    await cb.answer()


@mgr_r.message(AdmAmt.amount, F.text)
async def bal_apply(m: Message, bot: Bot, state: FSMContext):
    try:
        amt = round(float(m.text.strip()), 2)
        if amt <= 0:
            raise ValueError
    except ValueError:
        return await m.answer("❌ Positive number bhejo.")
    d = await state.get_data()
    uid, mode = d["uid"], d["mode"]
    await state.clear()
    if mode == "ab":
        await add_bal(uid, amt)
        await notify(bot, uid, "💰 Wallet me {} add hua.".format(money(amt)))
    else:
        if not await ded_bal(uid, amt):
            return await m.answer("❌ User ka balance kam hai.", reply_markup=to_panel())
        await notify(bot, uid, "💰 Wallet se {} deduct hua.".format(money(amt)))
    s, k = await ucard(uid)
    await m.answer(s, reply_markup=k)
    await audit(bot, m, "{} {} balance for {}".format("added" if mode == "ab" else "deducted", money(amt), uid))


# ---------- orders ----------
@mgr_r.callback_query(F.data == "adm:order")
async def order_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdmOrd.oid)
    await show(cb, "🧾 Full Order ID bhejo (log channel me milti hai).\n/cancel to abort", to_panel())
    await cb.answer()


@mgr_r.message(AdmOrd.oid, F.text)
async def order_lookup(m: Message, state: FSMContext):
    oid = m.text.strip()
    o = await dget(f"orders/{oid}") if re.fullmatch(r"[A-Za-z0-9_-]{10,40}", oid) else None
    if not o:
        return await m.answer("❌ Order nahi mila. Dobara bhejo ya /cancel.")
    await state.clear()
    s = "🧾 <code>{}</code>\n👤 <code>{}</code>\n🌍 {}\n📞 {}\n💰 {}\nStatus: {}\n🔢 OTP tries: {}".format(
        oid, o["uid"], esc(o["country"]), esc(o["phone"]), money(o["price"]), o["status"], o.get("otpCount", 0))
    rows = []
    if o["status"] in ("waiting_otp", "completed"):
        rows.append([B(text="↩️ Force Refund", callback_data="fr:" + oid)])
    rows.append([B(text="⬅️ Admin Panel", callback_data="adm:main")])
    await m.answer(s, reply_markup=kb(rows))


@mgr_r.callback_query(F.data.startswith("fr:"))
async def force_refund(cb: CallbackQuery, bot: Bot):
    oid = cb.data[3:]
    ok = await refund_order(bot, oid, "Admin ne refund kiya.", force=True)
    await cb.answer("✅ Refunded" if ok else "Already refunded/closed.", show_alert=True)
    if ok:
        await audit(bot, cb, "force refund " + oid)


# ---------- deposits ----------
async def _claim_dep(did, status, admin_id):
    box = {"ok": False}

    def f(cur):
        if cur and cur.get("status") == "pending":
            cur["status"] = status
            cur["by"] = admin_id
            box["ok"] = True
        else:
            box["ok"] = False
        return cur

    await dtx(f"deposits/{did}", f)
    return box["ok"]


async def mark_msg(cb, extra):
    try:
        base = cb.message.html_text or ""
        if cb.message.photo:
            await cb.message.edit_caption(caption=base + extra)
        else:
            await cb.message.edit_text(base + extra)
    except Exception as e:
        LOG.warning("mark_msg: %s", e)


@mgr_r.callback_query(F.data.startswith("dap:"))
async def dep_ok(cb: CallbackQuery, bot: Bot):
    did = cb.data[4:]
    if not await approve_deposit(bot, did, cb.from_user.id):
        return await cb.answer("Already processed.", show_alert=True)
    d = await dget(f"deposits/{did}") or {}
    await mark_msg(cb, "\n\n✅ <b>Approved</b> by <code>{}</code>".format(cb.from_user.id))
    await cb.answer("Approved")
    await audit(bot, cb, "approved deposit {} ({})".format(did[-6:], money(d.get("amount", 0))))


@mgr_r.callback_query(F.data.startswith("drj:"))
async def dep_no(cb: CallbackQuery, bot: Bot):
    did = cb.data[4:]
    if not await _claim_dep(did, "rejected", cb.from_user.id):
        return await cb.answer("Already processed.", show_alert=True)
    d = await dget(f"deposits/{did}")
    await notify(bot, d["uid"], "❌ Aapka {} ka deposit reject ho gaya. Support se contact karo.".format(money(d["amount"])))
    await mark_msg(cb, "\n\n❌ <b>Rejected</b> by <code>{}</code>".format(cb.from_user.id))
    await cb.answer("Rejected")
    await audit(bot, cb, "rejected deposit " + did[-6:])


# ---------- broadcast ----------
@mgr_r.callback_query(F.data == "adm:bc")
async def bc_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdmBC.msg)
    await show(cb, "📢 Broadcast message bhejo (text/photo/video sab chalega).\n/cancel to abort", to_panel())
    await cb.answer()


async def do_broadcast(bot: Bot, src: Message):
    sent = blocked = failed = 0
    for uid in await dkeys("users"):
        try:
            await src.copy_to(int(uid))
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await src.copy_to(int(uid))
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            blocked += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await notify(bot, src.from_user.id, "📢 Broadcast done\n✅ Sent: {}\n🚫 Blocked: {}\n⚠️ Failed: {}".format(sent, blocked, failed))


_bg = set()


@mgr_r.message(AdmBC.msg)
async def bc_run(m: Message, bot: Bot, state: FSMContext):
    await state.clear()
    task = asyncio.create_task(do_broadcast(bot, m))
    _bg.add(task)
    task.add_done_callback(_bg.discard)
    await m.answer("📢 Broadcast start ho gaya. Complete hone par report milegi.", reply_markup=to_panel())
    await audit(bot, m, "started broadcast")


# ---------- promo codes ----------
@mgr_r.callback_query(F.data == "adm:promo")
async def admin_promo_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdmPromo.data)
    await show(cb, ("🏷 <b>Create promo code</b>\n\nFormat: <code>CODE|amount|max_uses</code>\n"
                    "Example: <code>WELCOME50|50|100</code>\n(max_uses 0 = unlimited)\n/cancel to abort"), to_panel())
    await cb.answer()


@mgr_r.message(AdmPromo.data, F.text)
async def promo_create(m: Message, state: FSMContext, bot: Bot):
    parts = [p.strip() for p in m.text.split("|")]
    try:
        code = re.sub(r"[^A-Z0-9]", "", parts[0].upper())
        amount = float(parts[1])
        mx = int(parts[2]) if len(parts) > 2 else 0
        if not code or amount <= 0 or mx < 0:
            raise ValueError
    except (ValueError, IndexError):
        return await m.answer("❌ Format: <code>CODE|amount|max_uses</code>")
    await state.clear()
    await dset(f"promos/{code}", {"amount": amount, "max": mx, "used": 0})
    await m.answer("✅ Promo <code>{}</code> created: {} (max {}).".format(code, money(amount), mx or "∞"), reply_markup=to_panel())
    await audit(bot, m, "created promo {}".format(code))


# ---------- export ----------
@mgr_r.callback_query(F.data == "adm:export")
async def export_menu(cb: CallbackQuery):
    rows = [[B(text="🧾 Orders", callback_data="exp:orders"), B(text="👥 Users", callback_data="exp:users")],
            [B(text="💳 Deposits", callback_data="exp:deposits")], [B(text="⬅️ Admin Panel", callback_data="adm:main")]]
    await show(cb, "📤 <b>Export CSV</b>", kb(rows))
    await cb.answer()


@mgr_r.callback_query(F.data.in_({"exp:orders", "exp:users", "exp:deposits"}))
async def export_run(cb: CallbackQuery, bot: Bot):
    kind = cb.data[4:]
    await cb.answer("⏳ Preparing...")
    data = await dget(kind) or {}
    if kind == "orders":
        f = csv_file("orders.csv", ["id", "uid", "country", "phone", "price", "status", "createdAt"],
                     [[k, v.get("uid"), v.get("country"), v.get("phone"), v.get("price"), v.get("status"), v.get("createdAt")] for k, v in data.items()])
    elif kind == "users":
        f = csv_file("users.csv", ["uid", "name", "username", "balance", "spent", "orders", "refs", "banned"],
                     [[k, v.get("name"), v.get("username"), v.get("balance"), v.get("spent"), v.get("orders"), v.get("refs", 0), v.get("banned")] for k, v in data.items()])
    else:
        f = csv_file("deposits.csv", ["id", "uid", "amount", "method", "ref", "status", "createdAt"],
                     [[k, v.get("uid"), v.get("amount"), v.get("method"), v.get("ref"), v.get("status"), v.get("createdAt")] for k, v in data.items()])
    await bot.send_document(cb.from_user.id, f, caption="📤 {}.csv".format(kind))
    await audit(bot, cb, "exported " + kind)


# ---------- add account via bot login ----------
LOGIN = {}


async def drop_login(uid):
    d = LOGIN.pop(uid, None)
    if d and d.get("client"):
        await _close(d["client"])


@mgr_r.callback_query(F.data == "adm:login")
async def login_start(cb: CallbackQuery, state: FSMContext):
    await drop_login(cb.from_user.id)
    await state.set_state(AddAcc.info)
    await show(cb, ("➕ <b>Add Account (Bot login)</b>\n\nPehle country aur price bhejo:\n"
                    "<code>country|price</code>\nExample: <code>🇮🇳 India|45</code>\n/cancel to abort"), to_panel())
    await cb.answer()


@mgr_r.message(AddAcc.info, F.text)
async def login_info(m: Message, state: FSMContext):
    parts = [x.strip() for x in m.text.split("|")]
    try:
        country, price = parts[0], float(parts[1])
        if not country or price <= 0:
            raise ValueError
    except (ValueError, IndexError):
        return await m.answer("❌ Format: <code>country|price</code>")
    await state.update_data(country=country, price=price)
    await state.set_state(AddAcc.phone)
    await m.answer("📞 Account ka phone number bhejo (country code ke saath), jaise <code>+919876543210</code>")


@mgr_r.message(AddAcc.phone, F.text)
async def login_phone(m: Message, state: FSMContext):
    phone = "+" + re.sub(r"\D", "", m.text)
    if len(phone) < 8:
        return await m.answer("❌ Sahi number bhejo.")
    await drop_login(m.from_user.id)
    client = _tg_client(None)
    try:
        await asyncio.wait_for(client.connect(), 30)
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await _close(client)
        return await m.answer("❌ Number invalid hai. Dobara bhejo ya /cancel.")
    except FloodWaitError as e:
        await _close(client)
        await state.clear()
        return await m.answer("⏳ Telegram ne {} second ka wait lagaya, baad me try karo.".format(e.seconds), reply_markup=to_panel())
    except Exception as e:
        await _close(client)
        await state.clear()
        LOG.warning("send_code_request: %s", e)
        return await m.answer("❌ Code bhejne me error: {}".format(esc(type(e).__name__)), reply_markup=to_panel())
    LOGIN[m.from_user.id] = {"client": client, "phone": phone, "hash": sent.phone_code_hash}
    await state.set_state(AddAcc.code)
    await m.answer("📩 Telegram ne <code>{}</code> par login code bheja hai (us account ke Telegram app me / SMS me).\n\n"
                   "Code yahan bhejo <b>spaces ke saath</b>, jaise <code>1 2 3 4 5</code>.\n"
                   "(Seedha 12345 bhejoge to Telegram code block kar sakta hai.)\n/cancel to abort".format(esc(phone)))


async def login_finish(m: Message, state: FSMContext):
    d = LOGIN.get(m.from_user.id)
    try:
        sess = d["client"].session.save()
        me = await d["client"].get_me()
    except Exception as e:
        await drop_login(m.from_user.id)
        await state.clear()
        return await m.answer("❌ Login finish nahi hua: {}".format(esc(type(e).__name__)), reply_markup=to_panel())
    d["sess"] = sess
    d["phone"] = ("+" + me.phone) if me and me.phone else d["phone"]
    await _close(d["client"])
    d["client"] = None
    await state.set_state(AddAcc.desc)
    s = "✅ Login ho gaya: <code>{}</code>".format(esc(d["phone"]))
    if d.get("tfa"):
        s += "\n🔑 2FA password save ho gaya."
    s += "\n\n📝 Ab account ki <b>description</b> likho (buyer ko dikhegi). Skip karne ke liye <code>-</code> bhejo."
    await m.answer(s)


@mgr_r.message(AddAcc.code, F.text)
async def login_code(m: Message, state: FSMContext):
    d = LOGIN.get(m.from_user.id)
    if not d or not d.get("client"):
        await state.clear()
        return await m.answer("Session expire ho gaya, dobara shuru karo.", reply_markup=to_panel())
    code = re.sub(r"\D", "", m.text)
    if not code:
        return await m.answer("❌ Code bhejo.")
    try:
        await d["client"].sign_in(phone=d["phone"], code=code, phone_code_hash=d["hash"])
    except SessionPasswordNeededError:
        await state.set_state(AddAcc.pwd)
        return await m.answer("🔑 Is account me 2FA laga hai. 2FA password bhejo:")
    except PhoneCodeInvalidError:
        return await m.answer("❌ Code galat hai. Dobara bhejo.")
    except PhoneCodeExpiredError:
        try:
            sent = await d["client"].send_code_request(d["phone"])
            d["hash"] = sent.phone_code_hash
            return await m.answer("⌛ Code expire ho gaya, naya code bheja hai. Wo bhejo (spaces ke saath).")
        except Exception as e:
            await drop_login(m.from_user.id)
            await state.clear()
            return await m.answer("❌ Naya code nahi bhej paya: {}".format(esc(type(e).__name__)), reply_markup=to_panel())
    except Exception as e:
        await drop_login(m.from_user.id)
        await state.clear()
        return await m.answer("❌ Login fail: {}".format(esc(type(e).__name__)), reply_markup=to_panel())
    await login_finish(m, state)


@mgr_r.message(AddAcc.pwd, F.text)
async def login_pwd(m: Message, state: FSMContext):
    d = LOGIN.get(m.from_user.id)
    if not d or not d.get("client"):
        await state.clear()
        return await m.answer("Session expire ho gaya, dobara shuru karo.", reply_markup=to_panel())
    pwd = m.text.strip()
    try:
        await d["client"].sign_in(password=pwd)
    except PasswordHashInvalidError:
        return await m.answer("❌ 2FA password galat hai. Dobara bhejo.")
    except Exception as e:
        await drop_login(m.from_user.id)
        await state.clear()
        return await m.answer("❌ Login fail: {}".format(esc(type(e).__name__)), reply_markup=to_panel())
    d["tfa"] = pwd
    try:
        await m.delete()
    except Exception:
        pass
    await login_finish(m, state)


@mgr_r.message(AddAcc.desc, F.text)
async def login_desc(m: Message, state: FSMContext, bot: Bot):
    d = LOGIN.pop(m.from_user.id, None)
    data = await state.get_data()
    await state.clear()
    if not d or not d.get("sess"):
        return await m.answer("❌ Session expire ho gaya, dobara shuru karo.", reply_markup=to_panel())
    desc = "" if m.text.strip() == "-" else m.text.strip()[:300]
    r = await add_item(data["country"], data["price"], d["phone"], d["sess"], d.get("tfa", ""), desc)
    if r == "dup":
        return await m.answer("⚠️ Ye number already stock me hai.", reply_markup=to_panel())
    await m.answer("✅ Added: <code>{}</code>\n🌍 {} • {}{}{}".format(
        esc(d["phone"]), esc(data["country"]), money(data["price"]),
        "\n🔑 2FA saved" if d.get("tfa") else "", "\n📝 " + esc(desc) if desc else ""), reply_markup=to_panel())
    await audit(bot, m, "added account {} via login".format(mask_phone(d["phone"])))


# ---------- channels (force-join + notification) ----------
@mgr_r.callback_query(F.data == "adm:chan")
async def chan_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    ch = (await cfg(True)).get("channels") or {}
    rows = [[B(text="➕ Force-Join Channel", callback_data="chan:add:force")],
            [B(text="➕ Notification Channel", callback_data="chan:add:feed")]]
    for k, v in (ch.get("force") or {}).items():
        rows.append([B(text="❌ Join: " + (v.get("title") or "")[:24], callback_data="chan:del:force:" + k)])
    for k, v in (ch.get("feed") or {}).items():
        rows.append([B(text="❌ Feed: " + (v.get("title") or "")[:24], callback_data="chan:del:feed:" + k)])
    rows.append([B(text="⬅️ Admin Panel", callback_data="adm:main")])
    await show(cb, ("📣 <b>Channels</b>\n\n• <b>Force-Join</b>: user ko bot use karne se pehle join karna padega\n"
                    "• <b>Notification</b>: deposit / purchase / welcome posts (details hide ke saath) yahan jayengi\n\n"
                    "Pehle bot ko channel me <b>admin</b> banao, phir ➕ dabao."), kb(rows))
    await cb.answer()


@mgr_r.callback_query(F.data.regexp(r"^chan:add:(force|feed)$"))
async def chan_add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdmChan.ref)
    await state.update_data(kind=cb.data.split(":")[2])
    await show(cb, ("📣 Channel ka <b>@username</b> ya <b>ID</b> (-100...) bhejo, ya channel ka koi message "
                    "yahan <b>forward</b> karo.\n/cancel to abort"), to_panel())
    await cb.answer()


@mgr_r.message(AdmChan.ref)
async def chan_add_apply(m: Message, state: FSMContext, bot: Bot):
    kind = (await state.get_data()).get("kind", "feed")
    ref = None
    fo = getattr(m, "forward_origin", None)
    if fo is not None and getattr(fo, "chat", None) is not None:
        ref = fo.chat.id
    elif getattr(m, "forward_from_chat", None) is not None:
        ref = m.forward_from_chat.id
    elif m.text:
        tx = m.text.strip()
        ref = int(tx) if re.fullmatch(r"-?\d+", tx) else (tx if tx.startswith("@") else "@" + tx)
    if ref is None:
        return await m.answer("❌ Username/ID bhejo ya channel ka message forward karo.")
    try:
        chat = await bot.get_chat(ref)
    except Exception:
        return await m.answer("❌ Channel nahi mila. Bot ko channel me admin banao aur sahi @username/ID bhejo.")
    try:
        mem = await bot.get_chat_member(chat.id, BOT_USER["id"])
        is_admin = mem.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if not is_admin:
        return await m.answer("❌ Bot is channel me <b>admin</b> nahi hai. Pehle admin banao, phir dobara bhejo.")
    link = ("https://t.me/" + chat.username) if chat.username else None
    if kind == "force" and not link:
        try:
            link = await bot.export_chat_invite_link(chat.id)
        except Exception:
            link = None
    if kind == "feed":
        try:
            await bot.send_message(chat.id, "✅ Notification channel connected!")
        except Exception as e:
            return await m.answer("❌ Is channel me post nahi kar paya ({}). Bot ko post karne ka permission do.".format(esc(type(e).__name__)))
    key = str(chat.id).replace("-", "m")
    await dset(f"settings/channels/{kind}/{key}", {"id": chat.id, "title": chat.title or str(chat.id), "link": link})
    await cfg(True)
    await state.clear()
    await m.answer("✅ {} channel add ho gaya: <b>{}</b>".format("Force-Join" if kind == "force" else "Notification", esc(chat.title)),
                   reply_markup=kb([[B(text="📣 Channels", callback_data="adm:chan")]]))
    await audit(bot, m, "added {} channel {}".format(kind, esc(chat.title)))


@mgr_r.callback_query(F.data.regexp(r"^chan:del:(force|feed):[\w]+$"))
async def chan_del(cb: CallbackQuery, state: FSMContext, bot: Bot):
    _, _, kind, key = cb.data.split(":")
    await ddel(f"settings/channels/{kind}/{key}")
    await cfg(True)
    await audit(bot, cb, "removed {} channel {}".format(kind, key))
    await chan_menu(cb, state)


# ---------- UPI change ----------
@mgr_r.callback_query(F.data == "adm:upi")
async def upi_start(cb: CallbackQuery, state: FSMContext):
    upi, name = await upi_info()
    await state.set_state(AdmUpi.data)
    await show(cb, ("🏦 <b>UPI</b>\n\nAbhi: <code>{}</code> ({})\n\nNaya bhejo:\n<code>upi_id|name</code>\n"
                    "Example: <code>shop@upi|My Store</code>\n(name na likho to purana naam rahega)\n/cancel to abort").format(esc(upi), esc(name)), to_panel())
    await cb.answer()


@mgr_r.message(AdmUpi.data, F.text)
async def upi_apply(m: Message, state: FSMContext, bot: Bot):
    parts = [x.strip() for x in m.text.split("|")]
    if not re.fullmatch(r"[\w.\-]{2,}@[\w.\-]{2,}", parts[0]):
        return await m.answer("❌ UPI ID sahi nahi hai (example: <code>name@bank</code>).")
    _, cur_name = await upi_info()
    name = parts[1] if len(parts) > 1 and parts[1] else cur_name
    await dset("settings/upi", {"id": parts[0], "name": name})
    await cfg(True)
    await state.clear()
    await m.answer("✅ UPI update ho gaya: <code>{}</code> ({})".format(esc(parts[0]), esc(name)), reply_markup=to_panel())
    await audit(bot, m, "changed UPI to {}".format(esc(parts[0])))


# ---------- welcome message ----------
@mgr_r.callback_query(F.data == "adm:welcome")
async def welcome_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdmWelcome.text)
    await show(cb, ("✏️ <b>Welcome message</b>\n\nNaya welcome message bhejo (bold/italic Telegram se hi lagao).\n"
                    "Placeholders: <code>{user}</code> = user ka naam, <code>{bot}</code> = bot ka naam\n"
                    "<code>default</code> bhejo to original wapas.\n/cancel to abort"), to_panel())
    await cb.answer()


@mgr_r.message(AdmWelcome.text, F.text)
async def welcome_apply(m: Message, state: FSMContext, bot: Bot):
    txt = m.html_text
    if m.text.strip().lower() == "default":
        await ddel("settings/welcome")
        await cfg(True)
        await state.clear()
        return await m.answer("✅ Default welcome message wapas set.", reply_markup=to_panel())
    try:
        await m.answer(txt.replace("{user}", esc(m.from_user.first_name)).replace("{bot}", esc(BOT_NAME)))
    except TelegramBadRequest:
        return await m.answer("❌ Message ka formatting galat hai, dobara bhejo.")
    await dset("settings/welcome", txt)
    await cfg(True)
    await state.clear()
    await m.answer("✅ Welcome message save ho gaya (upar preview hai).", reply_markup=to_panel())
    await audit(bot, m, "changed welcome message")


# ---------- auto-pay status ----------
@mgr_r.callback_query(F.data == "adm:mail")
async def mail_status(cb: CallbackQuery):
    rows = []
    if not MAIL_ON:
        txt = ("📬 <b>Auto-Pay band hai</b>\n\n.env me <code>GMAIL_USER</code> aur <code>GMAIL_APP_PASSWORD</code> daalo, "
               "phir bot restart karo.")
    else:
        allowed = await allowed_domains()
        ago = "{}s pehle".format(now() - MAILSTAT["last"]) if MAILSTAT["last"] else "abhi tak nahi"
        txt = ("📬 <b>Auto-Pay ON</b>\n📧 {}\n✅ Allowed sender domains: <code>{}</code>\n🛡 DKIM check: {}\n"
               "⏱ Last check: {}\n📨 Payment mails dekhe: {}\n💰 Auto limit: {}\n".format(
                   esc(GMAIL_USER), esc(", ".join(allowed)), "ON" if MAIL_REQUIRE_DKIM else "OFF",
                   ago, MAILSTAT["seen"], money(AUTO_MAX)))
        if MAILSTAT["err"]:
            txt += "\n⚠️ Error: <code>{}</code>\n".format(esc(MAILSTAT["err"]))
        rec = MAILSTAT["recent"][-5:][::-1]
        if rec:
            txt += "\n<b>Recent payment mails:</b>\n"
            for r in rec:
                txt += "• {} | {} | UTR {} | {}{}\n".format(
                    esc(r.get("dom", "?")), ", ".join(money(a) for a in r["amounts"]) or "amount?", ", ".join(r["utrs"]) or "?",
                    "credit" if r["credit"] else "not-credit", " | ❌ " + r["skip"] if r["skip"] else " | ✅")
        else:
            txt += "\nAbhi tak allowed sender ki koi mail nahi mili."
        others = [d for d in MAILSTAT["others"] if len(d) <= 40]
        others.sort(key=lambda d: (0 if ("fam" in d or "trio" in d) else 1, d))
        if others:
            txt += "\n<b>Inbox me in senders ki mails mili (allowed nahi):</b>\n" + ", ".join(esc(d) for d in others[:10])
            txt += "\nFamApp ki mail jis domain se aati hai wo neeche tap karke allow karo (sirf owner)."
            if role_of(cb.from_user.id) == "owner":
                for d in others[:6]:
                    rows.append([B(text="✅ Allow " + d, callback_data="mail:allow:" + d)])
    rows.append([B(text="⬅️ Admin Panel", callback_data="adm:main")])
    await show(cb, txt, kb(rows))
    await cb.answer()


@own_r.callback_query(F.data.regexp(r"^mail:allow:[a-z0-9.\-]{3,40}$"))
async def mail_allow(cb: CallbackQuery, bot: Bot):
    dom = cb.data[len("mail:allow:"):]
    cur = (await cfg(True)).get("mail_domains") or ""
    doms = sorted(set([x for x in cur.split(",") if x] + [dom]))
    await dset("settings/mail_domains", ",".join(doms))
    await cfg(True)
    MAILSTATE["last_uid"] = 0   # naye allowed domain ki purani mails dobara scan hongi
    await audit(bot, cb, "allowed mail sender domain " + esc(dom))
    await cb.answer("✅ Allowed: " + dom, show_alert=True)
    await mail_status(cb)


# ---------- owner: admins / backup ----------
@own_r.callback_query(F.data == "adm:admins")
async def admins_view(cb: CallbackQuery):
    await load_admins(True)
    rows = [[B(text="❌ {} ({})".format(k, v), callback_data="radm:{}".format(k))] for k, v in _admins["d"].items()]
    rows.append([B(text="➕ Add Admin", callback_data="adm:addadmin")])
    rows.append([B(text="⬅️ Admin Panel", callback_data="adm:main")])
    await show(cb, "🛡 <b>Admins</b>\nOwner: {}\nTap to remove 👇".format(", ".join(str(x) for x in ADMIN_IDS)), kb(rows))
    await cb.answer()


@own_r.callback_query(F.data == "adm:addadmin")
async def admin_add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdmAdmins.data)
    await show(cb, ("🛡 Format: <code>user_id role</code>\nRoles: <code>manager</code> (stock/users/deposits...) "
                    "ya <code>support</code> (sirf tickets)\n/cancel to abort"), to_panel())
    await cb.answer()


@own_r.message(AdmAdmins.data, F.text)
async def admin_add_apply(m: Message, state: FSMContext, bot: Bot):
    parts = m.text.split()
    if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in ("manager", "support"):
        return await m.answer("❌ Format: <code>user_id manager|support</code>")
    await state.clear()
    await dset(f"admins/{parts[0]}", parts[1])
    await load_admins(True)
    await m.answer("✅ {} ab {} hai.".format(parts[0], parts[1]), reply_markup=to_panel())
    await notify(bot, int(parts[0]), "🛡 Aapko <b>{}</b> role mila hai. /admin se panel kholo.".format(parts[1]))
    await audit(bot, m, "set {} as {}".format(parts[0], parts[1]))


@own_r.callback_query(F.data.regexp(r"^radm:\d+$"))
async def admin_remove(cb: CallbackQuery, bot: Bot):
    uid = cb.data[5:]
    await ddel(f"admins/{uid}")
    await load_admins(True)
    await cb.answer("Removed")
    await audit(bot, cb, "removed admin " + uid)
    await admins_view(cb)


@own_r.callback_query(F.data == "adm:backup")
async def backup_now(cb: CallbackQuery, bot: Bot):
    await cb.answer("⏳ Backup bana raha hu...")
    await send_backup(bot, [cb.from_user.id])


# ==================== MAIN ====================
async def retry_forever(fn, what):
    """Network (DNS) issue par bot band na ho, khud dobara try kare."""
    delay = 5
    while True:
        try:
            return await fn()
        except TelegramUnauthorizedError:
            LOG.error("BOT_TOKEN galat hai. .env me sahi token daalo.")
            sys.exit(1)
        except Exception as e:
            LOG.warning("%s fail (%s). %ss baad retry...", what, type(e).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def start_web():
    """Render jaise hosts ke liye: PORT par chhota health server (BotHost par PORT nahi hota to ye skip hota hai)."""
    from aiohttp import web

    async def ok(_):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT"))).start()
    LOG.info("Health server on port %s", os.getenv("PORT"))


async def keepalive(url):
    """Render free plan 15 min inactivity par sula deta hai; ye apne URL ko ping karke jagaye rakhta hai."""
    import aiohttp
    await asyncio.sleep(60)
    while True:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url + "/health", timeout=aiohttp.ClientTimeout(total=20)) as r:
                    await r.read()
        except Exception as e:
            LOG.warning("keepalive ping fail: %s", type(e).__name__)
        await asyncio.sleep(600)


async def main():
    check_config()
    if os.getenv("PORT"):
        await start_web()
    init_services()
    bot = SafeBot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await retry_forever(bot.get_me, "Telegram connect")
    BOT_USER["n"] = me.username or ""
    BOT_USER["id"] = me.id
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(Gate())
    dp.include_router(user_r)
    dp.include_router(staff_r)
    dp.include_router(mgr_r)
    dp.include_router(own_r)
    await load_admins(True)
    await cfg(True)
    await build_premium(bot)
    await calibrate_buttons(bot)
    tasks = [asyncio.create_task(watcher(bot)), asyncio.create_task(backup_loop(bot))]
    if MAIL_ON:
        tasks.append(asyncio.create_task(mail_loop(bot)))
    if env("RENDER_EXTERNAL_URL"):
        tasks.append(asyncio.create_task(keepalive(env("RENDER_EXTERNAL_URL").rstrip("/"))))
    await retry_forever(lambda: bot.delete_webhook(drop_pending_updates=True), "delete_webhook")
    LOG.info("Bot started as @%s", BOT_USER["n"])
    try:
        await dp.start_polling(bot)
    finally:
        for tk in tasks:
            tk.cancel()


if __name__ == "__main__":
    asyncio.run(main())
