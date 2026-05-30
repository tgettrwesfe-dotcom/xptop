import discord
from discord.ext import commands, tasks
import sqlite3
import datetime
import re
import hashlib
from collections import defaultdict
import asyncio

# =============================================
#   اعدادات البوت - غيّر التوكن هنا
# =============================================
TOKEN = "ضع_توكن_البوت_هنا"
PREFIX = "!"

# =============================================
#   اعدادات نظام XP
# =============================================
XP_COOLDOWN_SECONDS = 60        # ثانية بين كل رسالة تكسب XP
MIN_MESSAGE_LENGTH = 10         # أقل طول للرسالة تستاهل XP
XP_MIN = 5                      # أقل XP لرسالة
XP_MAX = 15                     # أكثر XP لرسالة
SPAM_SIMILARITY_THRESHOLD = 0.8 # نسبة التشابه اللي تعتبر سبام (80%)
MAX_MESSAGES_PER_MINUTE = 8     # أكثر رسايل بالدقيقة قبل الحظر المؤقت

# =============================================
#   اعداد قاعدة البيانات
# =============================================
def init_db():
    conn = sqlite3.connect("xp_data.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS xp (
            user_id TEXT PRIMARY KEY,
            username TEXT,
            total_xp INTEGER DEFAULT 0,
            daily_xp INTEGER DEFAULT 0,
            weekly_xp INTEGER DEFAULT 0,
            monthly_xp INTEGER DEFAULT 0,
            last_message_time TEXT,
            last_message_hash TEXT,
            last_reset_daily TEXT,
            last_reset_weekly TEXT,
            last_reset_monthly TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# =============================================
#   نظام الكاش للمعلومات المؤقتة (بالذاكرة)
# =============================================
# تاريخ وعدد الرسايل بالدقيقة لكل يوزر
message_rate = defaultdict(list)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# =============================================
#   دوال مساعدة
# =============================================

def get_now():
    return datetime.datetime.utcnow()

def message_hash(content: str) -> str:
    """هاش للرسالة لاكتشاف التكرار"""
    cleaned = re.sub(r'\s+', ' ', content.strip().lower())
    return hashlib.md5(cleaned.encode()).hexdigest()

def similarity_ratio(a: str, b: str) -> float:
    """نسبة التشابه بين رسالتين"""
    a = re.sub(r'\s+', ' ', a.strip().lower())
    b = re.sub(r'\s+', ' ', b.strip().lower())
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # مقارنة بسيطة بالحروف المشتركة
    longer = max(len(a), len(b))
    matches = sum(1 for x, y in zip(a, b) if x == y)
    return matches / longer

def calculate_xp(message_content: str) -> int:
    """يحسب XP بناءً على جودة الرسالة"""
    content = message_content.strip()
    
    # رسايل قصيرة تاخذ XP أقل
    if len(content) < MIN_MESSAGE_LENGTH:
        return 0
    
    # XP أساسي
    xp = XP_MIN
    
    # مكافأة على الطول (لكن بحد معين)
    length_bonus = min(len(content) // 20, 5)  # كل 20 حرف = 1 XP، ماكس 5
    xp += length_bonus
    
    # مكافأة على الكلمات المتنوعة
    words = set(content.lower().split())
    if len(words) >= 5:
        xp += 1
    if len(words) >= 10:
        xp += 2
    
    # عقوبة على الكابز لوك (ماحد يسولف بالكبير)
    upper_ratio = sum(1 for c in content if c.isupper()) / max(len(content), 1)
    if upper_ratio > 0.6:
        xp = max(XP_MIN, xp - 3)
    
    # حد اقصى
    xp = min(xp, XP_MAX)
    return xp

def get_or_create_user(conn, user_id: str, username: str):
    c = conn.cursor()
    c.execute("SELECT * FROM xp WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row:
        now = get_now().isoformat()
        c.execute("""
            INSERT INTO xp (user_id, username, total_xp, daily_xp, weekly_xp, monthly_xp,
                last_message_time, last_message_hash, last_reset_daily, last_reset_weekly, last_reset_monthly)
            VALUES (?, ?, 0, 0, 0, 0, ?, '', ?, ?, ?)
        """, (user_id, username, now, now, now, now))
        conn.commit()
        c.execute("SELECT * FROM xp WHERE user_id = ?", (user_id,))
        row = c.fetchone()
    return row

def reset_periods_if_needed(conn, user_id: str):
    """يريست الفترات التلقائي لكل يوزر"""
    c = conn.cursor()
    c.execute("SELECT daily_xp, weekly_xp, monthly_xp, last_reset_daily, last_reset_weekly, last_reset_monthly FROM xp WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row:
        return
    
    now = get_now()
    updates = {}
    
    # ريست يومي
    last_daily = datetime.datetime.fromisoformat(row[3])
    if (now - last_daily).days >= 1:
        updates['daily_xp'] = 0
        updates['last_reset_daily'] = now.isoformat()
    
    # ريست أسبوعي (كل 7 أيام)
    last_weekly = datetime.datetime.fromisoformat(row[4])
    if (now - last_weekly).days >= 7:
        updates['weekly_xp'] = 0
        updates['last_reset_weekly'] = now.isoformat()
    
    # ريست شهري (كل 30 يوم)
    last_monthly = datetime.datetime.fromisoformat(row[5])
    if (now - last_monthly).days >= 30:
        updates['monthly_xp'] = 0
        updates['last_reset_monthly'] = now.isoformat()
    
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        c.execute(f"UPDATE xp SET {set_clause} WHERE user_id = ?", (*updates.values(), user_id))
        conn.commit()

def is_spam(user_id: str, message_content: str, last_hash: str) -> tuple[bool, str]:
    """يكتشف السبام بأكثر من طريقة"""
    now = get_now()
    
    # فلتر معدل الرسايل
    rate_key = user_id
    # نظّف الرسايل القديمة (أكثر من دقيقة)
    message_rate[rate_key] = [t for t in message_rate[rate_key] if (now - t).seconds < 60]
    message_rate[rate_key].append(now)
    
    if len(message_rate[rate_key]) > MAX_MESSAGES_PER_MINUTE:
        return True, "سرعة رسايل عالية"
    
    # فلتر التكرار بالهاش
    current_hash = message_hash(message_content)
    if current_hash == last_hash:
        return True, "رسالة متكررة"
    
    # فلتر التشابه الكبير
    # (للتبسيط نستخدم الهاش - التشابه الكامل)
    return False, ""

# =============================================
#   استقبال الرسايل وحساب XP
# =============================================

@bot.event
async def on_ready():
    print(f"البوت شغّال: {bot.user}")
    auto_reset_check.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    
    await bot.process_commands(message)
    
    # تجاهل الأوامر
    if message.content.startswith(PREFIX):
        return
    
    user_id = str(message.author.id)
    username = str(message.author.display_name)
    content = message.content
    
    conn = sqlite3.connect("xp_data.db")
    row = get_or_create_user(conn, user_id, username)
    
    # ريست الفترات لو انتهت
    reset_periods_if_needed(conn, user_id)
    
    # أعد قراءة البيانات بعد الريست
    c = conn.cursor()
    c.execute("SELECT total_xp, daily_xp, weekly_xp, monthly_xp, last_message_time, last_message_hash FROM xp WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    
    total_xp, daily_xp, weekly_xp, monthly_xp, last_msg_time, last_msg_hash = row
    
    # فحص الكولداون
    last_time = datetime.datetime.fromisoformat(last_msg_time)
    now = get_now()
    if (now - last_time).seconds < XP_COOLDOWN_SECONDS:
        conn.close()
        return
    
    # فحص السبام
    is_spamming, reason = is_spam(user_id, content, last_msg_hash)
    if is_spamming:
        conn.close()
        return
    
    # حساب XP
    earned = calculate_xp(content)
    if earned <= 0:
        conn.close()
        return
    
    # تحديث قاعدة البيانات
    new_hash = message_hash(content)
    c.execute("""
        UPDATE xp SET
            username = ?,
            total_xp = total_xp + ?,
            daily_xp = daily_xp + ?,
            weekly_xp = weekly_xp + ?,
            monthly_xp = monthly_xp + ?,
            last_message_time = ?,
            last_message_hash = ?
        WHERE user_id = ?
    """, (username, earned, earned, earned, earned, now.isoformat(), new_hash, user_id))
    conn.commit()
    conn.close()

# =============================================
#   أمر التوب
# =============================================

@bot.command(name="top")
async def top_command(ctx, period: str = "all"):
    period = period.lower()
    periods = {
        "all": ("total_xp", "🏆 توب كل الوقت"),
        "daily": ("daily_xp", "☀️ توب اليوم"),
        "weekly": ("weekly_xp", "📅 توب الأسبوع"),
        "monthly": ("monthly_xp", "🗓️ توب الشهر"),
    }
    
    if period not in periods:
        await ctx.send("❌ اكتب: `!top` أو `!top daily` أو `!top weekly` أو `!top monthly`")
        return
    
    column, title = periods[period]
    
    conn = sqlite3.connect("xp_data.db")
    
    # ريست الفترات لكل المستخدمين قبل العرض
    c = conn.cursor()
    c.execute("SELECT user_id FROM xp")
    all_users = c.fetchall()
    for (uid,) in all_users:
        reset_periods_if_needed(conn, uid)
    
    c.execute(f"SELECT username, {column} FROM xp WHERE {column} > 0 ORDER BY {column} DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        await ctx.send(f"**{title}**\n\nما في بيانات لحد الآن!")
        return
    
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"**{title}**\n"]
    
    for i, (username, xp) in enumerate(rows):
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        lines.append(f"{medal} **{username}** — {xp:,} XP")
    
    await ctx.send("\n".join(lines))

# =============================================
#   أمر الرتبة الشخصية
# =============================================

@bot.command(name="rank")
async def rank_command(ctx, member: discord.Member = None):
    target = member or ctx.author
    user_id = str(target.id)
    username = str(target.display_name)
    
    conn = sqlite3.connect("xp_data.db")
    get_or_create_user(conn, user_id, username)
    reset_periods_if_needed(conn, user_id)
    
    c = conn.cursor()
    c.execute("SELECT total_xp, daily_xp, weekly_xp, monthly_xp FROM xp WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    
    # ترتيب الشخص
    c.execute("SELECT COUNT(*)+1 FROM xp WHERE total_xp > ?", (row[0],))
    rank = c.fetchone()[0]
    
    conn.close()
    
    embed = discord.Embed(
        title=f"📊 رتبة {username}",
        color=0x5865F2
    )
    embed.add_field(name="🏅 الترتيب الكلي", value=f"#{rank}", inline=True)
    embed.add_field(name="⭐ إجمالي XP", value=f"{row[0]:,}", inline=True)
    embed.add_field(name="☀️ اليوم", value=f"{row[1]:,} XP", inline=True)
    embed.add_field(name="📅 الأسبوع", value=f"{row[2]:,} XP", inline=True)
    embed.add_field(name="🗓️ الشهر", value=f"{row[3]:,} XP", inline=True)
    
    await ctx.send(embed=embed)

# =============================================
#   ريست يدوي (للأدمن فقط)
# =============================================

@bot.command(name="reset_daily")
@commands.has_permissions(administrator=True)
async def reset_daily_cmd(ctx):
    conn = sqlite3.connect("xp_data.db")
    now = get_now().isoformat()
    conn.execute("UPDATE xp SET daily_xp = 0, last_reset_daily = ?", (now,))
    conn.commit()
    conn.close()
    await ctx.send("✅ تم ريست توب اليوم!")

@bot.command(name="reset_weekly")
@commands.has_permissions(administrator=True)
async def reset_weekly_cmd(ctx):
    conn = sqlite3.connect("xp_data.db")
    now = get_now().isoformat()
    conn.execute("UPDATE xp SET weekly_xp = 0, last_reset_weekly = ?", (now,))
    conn.commit()
    conn.close()
    await ctx.send("✅ تم ريست توب الأسبوع!")

@bot.command(name="reset_monthly")
@commands.has_permissions(administrator=True)
async def reset_monthly_cmd(ctx):
    conn = sqlite3.connect("xp_data.db")
    now = get_now().isoformat()
    conn.execute("UPDATE xp SET monthly_xp = 0, last_reset_monthly = ?", (now,))
    conn.commit()
    conn.close()
    await ctx.send("✅ تم ريست توب الشهر!")

# =============================================
#   ريست تلقائي كل ساعة (فحص)
# =============================================

@tasks.loop(hours=1)
async def auto_reset_check():
    """يفحص كل ساعة إذا محتاج ريست"""
    conn = sqlite3.connect("xp_data.db")
    c = conn.cursor()
    c.execute("SELECT user_id FROM xp")
    all_users = c.fetchall()
    conn.close()
    
    conn = sqlite3.connect("xp_data.db")
    for (uid,) in all_users:
        reset_periods_if_needed(conn, uid)
    conn.close()

# =============================================
#   تشغيل البوت
# =============================================

bot.run(TOKEN)
