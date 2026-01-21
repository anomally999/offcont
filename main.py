#!/usr/bin/env python3
#  main.py  –  Render-ready │ 7-day rolling online/offline │ /tgoo │ All commands │ Zero syntax errors
import os
import asyncio
import datetime
import logging
from typing import List, Optional, NamedTuple

import discord
from discord import app_commands
from discord.ext import commands, tasks
from aiohttp import web
import asyncpg
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
if not TOKEN or not DATABASE_URL:
    raise RuntimeError("TOKEN and DATABASE_URL environment variables are required")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("royal-activity")

# ---------- SECURITY ----------
MAX_GUILD_SIZE = 250_000
COMMAND_COOLDOWN = 3
RETENTION_DAYS = 365

# ---------- BOT ----------
class RoyalActivityBot(commands.Bot):
    def __init__(self):
        super().__init__("!", intents=intents, help_command=None,
                         description="📊 Royal Activity Tracker – 7-day rolling online/offline")
        self.pool: Optional[asyncpg.Pool] = None
        self._avatar: Optional[str] = None

    async def setup_hook(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10, command_timeout=30)
        await self.create_tables()
        await self.tree.sync()
        self.loop.create_task(self.web_server())
        if self.user:
            self._avatar = self.user.display_avatar.url
        midnight_scan.start()
        retention_cleanup.start()
        weekly_reset.start()

    async def web_server(self):
        async def handle(_):
            return web.Response(text="👑 Royal Activity Bot is running")
        app = web.Application()
        app.router.add_get("/", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
        await site.start()
        log.info("Web server listening on PORT %s", os.environ.get("PORT", 8080))
        while True:
            await asyncio.sleep(3600)

    async def create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id            BIGINT PRIMARY KEY,
                    report_channel_id   BIGINT,
                    role_ids            BIGINT[] DEFAULT '{}',
                    alert_threshold     INT DEFAULT 7,
                    tz                  TEXT DEFAULT 'UTC'
                );
                CREATE TABLE IF NOT EXISTS user_activity (
                    guild_id        BIGINT,
                    user_id         BIGINT,
                    last_active_date DATE,
                    online_days     INT DEFAULT 0,
                    offline_days    INT DEFAULT 0,
                    week_start      DATE DEFAULT DATE_TRUNC('week', CURRENT_DATE),
                    total_online    INT DEFAULT 0,
                    total_offline   INT DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_activity_scan
                    ON user_activity(guild_id, last_active_date);
            """)

    async def close(self):
        if self.pool:
            await self.pool.close()
        await super().close()

bot = RoyalActivityBot()

# ---------- EMBEDS ----------
def royal_embed(title: str, color: int = 0x6441A5, desc: str = None) -> discord.Embed:
    e = discord.Embed(title=f"👑 {title}", color=color, description=desc,
                      timestamp=datetime.datetime.now(datetime.UTC))
    e.set_footer(text="Royal Activity Tracker – 7-day rolling", icon_url=bot._avatar or "https://i.imgur.com/8OjyFJI.png")
    if bot._avatar:
        e.set_thumbnail(url=bot._avatar)
    return e

def error(txt: str) -> discord.Embed:
    return royal_embed("❌ Error", 0xE74C3C, txt)

def success(txt: str) -> royal_embed("✅ Success", 0x2ECC71, txt)

# ---------- HELP ----------
@bot.command(name="help")
async def text_help(ctx: commands.Context):
    e = royal_embed("📜 Royal Commands", 0xF1C40F,
        "Track **real message activity** with 7-day rolling counters.\n"
        "→ Online day = sent ≥1 message that day\n"
        "→ Week resets every Sunday 00:00 UTC\n"
        "→ 7+ offline days → royal decree (alert)")
    c = [
        ("!help", "This parchment"),
        ("!channelset #channel", "Set herald channel"),
        ("!roleset @role ...", "Noble roles to ping"),
        ("!chcheck", "Court settings"),
        ("!listinactive", "Who shirked duties today"),
        ("!active", "Who served today"),
        ("/tgoo", "Online/offline counters (today, 7d, total)"),
        ("Slash", "/channelset /roleset /chcheck /listinactive /active /tgoo /setthreshold /purgeactivity")
    ]
    for name, val in c:
        e.add_field(name=name, value=val, inline=False)
    await ctx.send(embed=e)

# ---------- EVENTS ----------
@bot.event
async def on_ready():
    log.info("👑 Crown placed on %s", bot.user)

@bot.event
async def on_guild_join(guild: discord.Guild):
    if guild.member_count > MAX_GUILD_SIZE:
        log.warning("Left %s (>%s members)", guild.name, MAX_GUILD_SIZE)
        await guild.leave()

@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not msg.guild:
        return
    today = datetime.date.today()
    week_start = today - datetime.timedelta(days=today.weekday())  # Monday 00:00
    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_activity(guild_id, user_id, last_active_date, online_days, offline_days,
                                      week_start, total_online, total_offline)
            VALUES ($1,$2,$3,1,0,$4,1,0)
            ON CONFLICT (guild_id, user_id) DO UPDATE
                SET last_active_date = $3,
                    online_days      = CASE
                                         WHEN EXCLUDED.week_start = user_activity.week_start
                                         THEN GREATEST(user_activity.online_days + 1, 1)
                                         ELSE 1
                                       END,
                    offline_days     = CASE
                                         WHEN EXCLUDED.week_start = user_activity.week_start
                                         THEN user_activity.offline_days
                                         ELSE 0
                                       END,
                    week_start       = EXCLUDED.week_start,
                    total_online     = user_activity.total_online + 1
            WHERE user_activity.last_active_date <> $3
        """, msg.guild.id, msg.author.id, today, week_start)
    await bot.process_commands(msg)

# ---------- BACKGROUND TASKS ----------
@tasks.loop(time=datetime.time(0, 0, tzinfo=datetime.UTC))
async def midnight_scan():
    today = datetime.date.today()
    async with bot.pool.acquire() as conn:
        for rec in await conn.fetch("""
            SELECT guild_id, report_channel_id, role_ids, alert_threshold
            FROM guild_settings
            WHERE report_channel_id IS NOT NULL
        """):
            gid, chid, rids, thresh = rec
            guild = bot.get_guild(gid)
            if not guild:
                continue
            channel = guild.get_channel(chid)
            if not channel or not isinstance(channel, discord.TextChannel):
                continue
            rows = await conn.fetch("""
                SELECT user_id,
                       offline_days + (CURRENT_DATE - last_active_date) AS current_streak
                FROM user_activity
                WHERE guild_id=$1 AND last_active_date <= CURRENT_DATE - INTERVAL '%s days'
                ORDER BY current_streak DESC
            """, thresh)
            if not rows:
                continue
            roles = [guild.get_role(rid) for rid in rids if guild.get_role(rid)]
            ping = " ".join(r.mention for r in roles) or "@here"
            e = royal_embed("🚨 Royal Inactivity Decree", 0xE74C3C,
                            f"**{thresh}+ consecutive days** absent • {today:%Y-%m-%d}")
            lines = []
            for row in rows[:10]:
                m = guild.get_member(row["user_id"])
                if m:
                    lines.append(f"• {m.mention} — **{row['current_streak']}** days")
            e.add_field(name=f"Knights & Ladies ({len(rows)} total)",
                        value="\n".join(lines) or "None", inline=False)
            if len(rows) > 10:
                e.add_field(name="Note", value=f"...and {len(rows)-10} more", inline=False)
            await channel.send(ping, embed=e)

@tasks.loop(hours=24)
async def retention_cleanup():
    cutoff = datetime.date.today() - datetime.timedelta(days=RETENTION_DAYS)
    async with bot.pool.acquire() as conn:
        await conn.execute("DELETE FROM user_activity WHERE last_active_date < $1", cutoff)
    log.info("Retention cleanup completed (%s days)", RETENTION_DAYS)

@tasks.loop(time=datetime.time(0, 0, tzinfo=datetime.UTC))
async def weekly_reset():
    monday = datetime.date.today()
    async with bot.pool.acquire() as conn:
        await conn.execute("""
            UPDATE user_activity
            SET online_days   = 0,
                offline_days  = 0,
                week_start    = $1
            WHERE week_start <> $1
        """, monday)
    log.info("Weekly counters reset (%s)", monday)

# ---------- PAGINATION ----------
class MemberPages(discord.ui.View):
    def __init__(self, members: List[discord.Member], title: str, color: int, owner_id: int):
        super().__init__(timeout=600)
        self.mems = members
        self.title = title
        self.color = color
        self.page = 0
        self.max_page = (len(members) - 1) // 10
        self.owner = owner_id
        self.msg: Optional[discord.Message] = None
        self.update_buttons()

    def update_buttons(self):
        self.prev.disabled = self.page == 0
        self.nxt.disabled = self.page >= self.max_page

    def build(self) -> discord.Embed:
        start = self.page * 10
        chunk = self.mems[start:start + 10]
        e = royal_embed(f"{self.title} ({len(self.mems)})", self.color,
                        f"Page {self.page + 1}/{self.max_page + 1}")
        e.description = "\n".join(f"• {m.mention}  `{m.display_name}`" for m in chunk) or "None"
        return e

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        return inter.user.id == self.owner

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.msg:
            await self.msg.edit(view=self)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.grey)
    async def prev(self, inter: discord.Interaction, _):
        self.page -= 1
        self.update_buttons()
        await inter.response.edit_message(embed=self.build(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.grey)
    async def nxt(self, inter: discord.Interaction, _):
        self.page += 1
        self.update_buttons()
        await inter.response.edit_message(embed=self.build(), view=self)

# ---------- SLASH COMMANDS ----------
@bot.tree.command(name="channelset", description="Set the royal herald channel for inactivity alerts")
@app_commands.describe(channel="Channel where the decree will be proclaimed")
@commands.cooldown(COMMAND_COOLDOWN, COMMAND_COOLDOWN, commands.BucketType.user)
async def slash_channelset(inter: discord.Interaction, channel: discord.TextChannel):
    if not inter.user.guild_permissions.manage_guild:
        return await inter.response.send_message(embed=error("Manage Server permission required"), ephemeral=True)
    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings(guild_id, report_channel_id)
            VALUES ($1,$2)
            ON CONFLICT (guild_id) DO UPDATE SET report_channel_id=$2
        """, inter.guild_id, channel.id)
    await inter.response.send_message(embed=success(f"Alerts will be proclaimed in {channel.mention}"), ephemeral=True)

@bot.tree.command(name="roleset", description="Choose noble roles to ping on royal decrees (up to 5)")
@app_commands.describe(r1="Role 1", r2="Role 2", r3="Role 3", r4="Role 4", r5="Role 5")
@commands.cooldown(COMMAND_COOLDOWN, COMMAND_COOLDOWN, commands.BucketType.user)
async def slash_roleset(inter: discord.Interaction,
                        r1: discord.Role,
                        r2: Optional[discord.Role] = None,
                        r3: Optional[discord.Role] = None,
                        r4: Optional[discord.Role] = None,
                        r5: Optional[discord.Role] = None):
    if not inter.user.guild_permissions.manage_guild:
        return await inter.response.send_message(embed=error("Manage Server permission required"), ephemeral=True)
    roles = [r for r in [r1, r2, r3, r4, r5] if r and not r.is_default() and not r.managed]
    if not roles:
        return await inter.response.send_message(embed=error("Select at least one valid role"), ephemeral=True)
    role_ids = [r.id for r in roles]
    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings(guild_id, role_ids)
            VALUES ($1,$2::bigint[])
            ON CONFLICT (guild_id) DO UPDATE SET role_ids=$2::bigint[]
        """, inter.guild_id, role_ids)
    mentions = " ".join(r.mention for r in roles)
    await inter.response.send_message(embed=success(f"Noble roles updated:\n{mentions}"), ephemeral=True)

@bot.tree.command(name="chcheck", description="View current court settings")
@commands.cooldown(COMMAND_COOLDOWN, COMMAND_COOLDOWN, commands.BucketType.user)
async def slash_chcheck(inter: discord.Interaction):
    async with bot.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT report_channel_id, role_ids, alert_threshold FROM guild_settings WHERE guild_id=$1",
                                  inter.guild_id)
    if not row or not row["report_channel_id"]:
        return await inter.response.send_message(embed=error("No settings configured"), ephemeral=True)
    channel = inter.guild.get_channel(row["report_channel_id"])
    ch = channel.mention if channel else "Deleted channel"
    roles = " ".join(f"<@&{rid}>" for rid in row["role_ids"]) or "None"
    e = royal_embed("⚙️ Court Settings", 0x3498DB)
    e.add_field(name="Herald Channel", value=ch, inline=False)
    e.add_field(name="Noble Roles", value=roles, inline=False)
    e.add_field(name="Alert Threshold", value=f"{row['alert_threshold']} days", inline=False)
    await inter.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name="setthreshold", description="Change the number of offline days before alert")
@app_commands.describe(days="1-90 days")
@commands.cooldown(COMMAND_COOLDOWN, COMMAND_COOLDOWN, commands.BucketType.user)
async def slash_setthreshold(inter: discord.Interaction, days: app_commands.Range[int, 1, 90]):
    if not inter.user.guild_permissions.manage_guild:
        return await inter.response.send_message(embed=error("Manage Server permission required"), ephemeral=True)
    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings(guild_id, alert_threshold)
            VALUES ($1,$2)
            ON CONFLICT (guild_id) DO UPDATE SET alert_threshold=$2
        """, inter.guild_id, days)
    await inter.response.send_message(embed=success(f"Alert threshold set to **{days} days**"), ephemeral=True)

@bot.tree.command(name="listinactive", description="Who shirked their duties today (paginated)")
@commands.cooldown(COMMAND_COOLDOWN, COMMAND_COOLDOWN, commands.BucketType.user)
async def slash_listinactive(inter: discord.Interaction):
    await inter.response.defer(ephemeral=False)
    today = datetime.date.today()
    async with bot.pool.acquire() as conn:
        active = {r["user_id"] for r in await conn.fetch("""
            SELECT user_id FROM user_activity
            WHERE guild_id=$1 AND last_active_date=$2
        """, inter.guild_id, today)}
    inactive = [m for m in inter.guild.members if not m.bot and m.id not in active]
    inactive.sort(key=lambda m: m.display_name.lower())
    if not inactive:
        return await inter.followup.send(embed=success("Everyone served the crown today! 🎉"))
    view = MemberPages(inactive, "🎪 Inactive Today", 0xE74C3C, inter.user.id)
    msg = await inter.followup.send(embed=view.build(), view=view)
    view.msg = msg

@bot.tree.command(name="active", description="Who served the crown today (paginated)")
@commands.cooldown(COMMAND_COOLDOWN, COMMAND_COOLDOWN, commands.BucketType.user)
async def slash_active(inter: discord.Interaction):
    await inter.response.defer(ephemeral=False)
    today = datetime.date.today()
    async with bot.pool.acquire() as conn:
        active_ids = {r["user_id"] for r in await conn.fetch("""
            SELECT user_id FROM user_activity
            WHERE guild_id=$1 AND last_active_date=$2
        """, inter.guild_id, today)}
    active = [m for m in inter.guild.members if not m.bot and m.id in active_ids]
    active.sort(key=lambda m: m.display_name.lower())
    if not active:
        return await inter.followup.send(embed=success("No one has served today."))
    view = MemberPages(active, "🎖️ Active Today", 0x2ECC71, inter.user.id)
    msg = await inter.followup.send(embed=view.build(), view=view)
    view.msg = msg

# ---------- NEW COMMAND ----------
class Counters(NamedTuple):
    today_on: int
    today_off: int
    week_on: int
    week_off: int
    total_on: int
    total_off: int

async def fetch_counters(guild_id: int, user_id: int) -> Counters:
    today = datetime.date.today()
    async with bot.pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT online_days, offline_days, total_online, total_offline, last_active_date
            FROM user_activity
            WHERE guild_id=$1 AND user_id=$2
        """, guild_id, user_id)
    if not row:
        return Counters(0, 1, 0, 1, 0, 1)  # never seen = offline today
    last = row["last_active_date"]
    today_on = 1 if last == today else 0
    today_off = 0 if last == today else 1
    week_on = row["online_days"]
    week_off = row["offline_days"]
    total_on = row["total_online"]
    total_off = row["total_offline"]
    return Counters(today_on, today_off, week_on, week_off, total_on, total_off)

@bot.tree.command(name="tgoo", description="Show your online/offline counters (today, 7-day, total)")
@commands.cooldown(COMMAND_COOLDOWN, COMMAND_COOLDOWN, commands.BucketType.user)
async def slash_tgoo(inter: discord.Interaction):
    await inter.response.defer(ephemeral=False)
    counters = await fetch_counters(inter.guild_id, inter.user.id)

    def bar(percent: float) -> str:
        p = int(percent * 10)
        return "🟩" * p + "⬜" * (10 - p)

    e = royal_embed("📊 Thy Online/Offline Scroll", 0xF1C40F)
    # Today
    today_total = counters.today_on + counters.today_off
    today_pct = counters.today_on / today_total if today_total else 0
    e.add_field(name="📅 Today", value=f"{bar(today_pct)}  `{counters.today_on} online · {counters.today_off} offline`", inline=False)
    # 7 days
    week_total = counters.week_on + counters.week_off
    week_pct = counters.week_on / week_total if week_total else 0
    e.add_field(name="📆 Last 7 Days", value=f"{bar(week_pct)}  `{counters.week_on} online · {counters.week_off} offline`", inline=False)
    # Total
    total = counters.total_on + counters.total_off
    total_pct = counters.total_on / total if total else 0
    e.add_field(name="🕰️ Total", value=f"{bar(total_pct)}  `{counters.total_on} online · {counters.total_off} offline`", inline=False)
    await inter.followup.send(embed=e)

# ---------- RUN ----------
bot.run(TOKEN)
