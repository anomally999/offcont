#!/usr/bin/env python3
# main.py – Render-ready activity tracker (fixed & enhanced)
import os
import asyncio
import datetime
import logging
from typing import List, Optional

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
    raise RuntimeError("TOKEN and DATABASE_URL must be set")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("activity-bot")

# ---------- BOT ----------
class InactivityBot(commands.Bot):
    def __init__(self):
        super().__init__(",", intents=intents, help_command=None,
                         description="Server activity tracker – real messages only")
        self.pool: Optional[asyncpg.Pool] = None
        self._avatar: Optional[str] = None

    async def setup_hook(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10,
                                              command_timeout=30)
        await self.create_tables()
        await self.tree.sync()
        self.loop.create_task(self.web_server())
        if self.user:
            self._avatar = self.user.display_avatar.url

    async def web_server(self):
        async def handle(_):
            return web.Response(text="Bot is running")
        app = web.Application()
        app.router.add_get("/", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080)))
        await site.start()
        log.info("Web server alive")
        while True:
            await asyncio.sleep(3600)

    async def create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id            BIGINT PRIMARY KEY,
                    report_channel_id   BIGINT,
                    role_ids            BIGINT[] DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS user_activity (
                    guild_id        BIGINT,
                    user_id         BIGINT,
                    last_active_date DATE,
                    offline_streak  INT DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_activity_scan
                    ON user_activity(guild_id, last_active_date);
            """)

    async def close(self):
        if self.pool:
            await self.pool.close()
        await super().close()

bot = InactivityBot()

# ---------- EMBEDS ----------
def embed_base(title: str, color: int = 0x5865F2, desc: str = None) -> discord.Embed:
    e = discord.Embed(title=f"✦ {title} ✦", color=color,
                      description=desc,
                      timestamp=datetime.datetime.now(datetime.UTC))
    e.set_footer(text="Activity Tracker • Real messages only")
    if bot._avatar:
        e.set_thumbnail(url=bot._avatar)
    return e

def error(txt: str) -> discord.Embed:
    return embed_base("Error", 0xED4245, f"❌ {txt}")

def success(txt: str) -> discord.Embed:
    return embed_base("Success", 0x57F287, f"✅ {txt}")

# ---------- HELP ----------
@bot.command(name="helpactivity")
async def text_help(ctx: commands.Context):
    e = embed_base("Activity Tracker Commands", 0xFEE75C,
        "Tracks **real message activity** per server.\n"
        "→ Active = sent ≥1 message today\n"
        "→ 12 consecutive offline days → auto alert + ping")
    c = [
        (",helpactivity", "This menu"),
        (",channelset #channel", "Set alert channel"),
        (",roleset @role ...", "Roles to ping"),
        (",chcheck", "View current settings"),
        (",listinactive", "Who did **not** message today"),
        (",active", "Who **did** message today"),
        ("Slash", "/channelset /roleset /chcheck /listinactive /active")
    ]
    for name, val in c:
        e.add_field(name=name, value=val, inline=False)
    await ctx.send(embed=e)

# ---------- EVENTS ----------
@bot.event
async def on_ready():
    log.info("Ready as %s", bot.user)
    check_inactivity.start()

@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not msg.guild:
        return
    today = datetime.date.today()
    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_activity(guild_id, user_id, last_active_date, offline_streak)
            VALUES ($1,$2,$3,0)
            ON CONFLICT (guild_id, user_id) DO UPDATE
                SET last_active_date = EXCLUDED.last_active_date,
                    offline_streak   = 0
            WHERE user_activity.last_active_date <> $3
        """, msg.guild.id, msg.author.id, today)
    await bot.process_commands(msg)

# ---------- TASK ----------
@tasks.loop(time=datetime.time(0, 0, tzinfo=datetime.UTC))
async def check_inactivity():
    today = datetime.date.today()
    async with bot.pool.acquire() as conn:
        for rec in await conn.fetch("""
            SELECT guild_id, report_channel_id, role_ids
            FROM guild_settings
            WHERE report_channel_id IS NOT NULL
        """):
            gid, chid, rids = rec
            guild = bot.get_guild(gid)
            if not guild:
                continue
            channel = guild.get_channel(chid)
            if not channel or not isinstance(channel, discord.TextChannel):
                continue
            rows = await conn.fetch("""
                SELECT user_id,
                       offline_streak + (CURRENT_DATE - last_active_date) AS streak
                FROM user_activity
                WHERE guild_id=$1 AND last_active_date <= CURRENT_DATE - INTERVAL '12 days'
                ORDER BY streak DESC
            """, gid)
            if not rows:
                continue
            roles = [guild.get_role(rid) for rid in rids if guild.get_role(rid)]
            ping = " ".join(r.mention for r in roles) or "@here"
            e = embed_base("Long-term Inactive Members", 0xED4245,
                           f"**12+ days** offline • {today:%Y-%m-%d}")
            lines = []
            for r in rows[:12]:
                m = guild.get_member(r["user_id"])
                if m:
                    lines.append(f"• {m.mention} — **{r['streak']}** days")
            e.add_field(name=f"Members ({len(rows)} total)",
                        value="\n".join(lines) or "None", inline=False)
            if len(rows) > 12:
                e.add_field(name="Note", value=f"...and {len(rows)-12} more", inline=False)
            await channel.send(ping, embed=e)

# ---------- PAGINATION ----------
class MemberPages(discord.ui.View):
    def __init__(self, members: List[discord.Member], title: str, color: int):
        super().__init__(timeout=600)
        self.mems = members
        self.title = title
        self.color = color
        self.page = 0
        self.max_page = (len(members) - 1) // 15
        self.msg: Optional[discord.Message] = None
        self.update_buttons()

    def update_buttons(self):
        self.prev.disabled = self.page == 0
        self.nxt.disabled = self.page >= self.max_page

    def build(self) -> discord.Embed:
        start = self.page * 15
        chunk = self.mems[start:start + 15]
        e = embed_base(f"{self.title} ({len(self.mems)})", self.color,
                       f"Page {self.page + 1}/{self.max_page + 1}")
        e.description = "\n".join(f"• {m.mention} ({m.display_name})" for m in chunk) or "None"
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
@bot.tree.command(name="channelset")
@app_commands.describe(channel="Alert channel")
async def slash_channelset(inter: discord.Interaction, channel: discord.TextChannel):
    if not inter.user.guild_permissions.manage_guild:
        return await inter.response.send_message(embed=error("Manage Server required"), ephemeral=True)
    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings(guild_id, report_channel_id)
            VALUES ($1,$2)
            ON CONFLICT (guild_id) DO UPDATE SET report_channel_id=$2
        """, inter.guild_id, channel.id)
    await inter.response.send_message(embed=success(f"Alerts → {channel.mention}"), ephemeral=True)

@bot.tree.command(name="roleset")
@app_commands.describe(r1="Role 1", r2="Role 2", r3="Role 3", r4="Role 4", r5="Role 5")
async def slash_roleset(inter: discord.Interaction,
                        r1: discord.Role,
                        r2: Optional[discord.Role] = None,
                        r3: Optional[discord.Role] = None,
                        r4: Optional[discord.Role] = None,
                        r5: Optional[discord.Role] = None):
    if not inter.user.guild_permissions.manage_guild:
        return await inter.response.send_message(embed=error("Manage Server required"), ephemeral=True)
    roles = [r for r in [r1, r2, r3, r4, r5] if r]
    if not roles:
        return await inter.response.send_message(embed=error("Pick at least one role"), ephemeral=True)
    role_ids = [r.id for r in roles]
    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings(guild_id, role_ids)
            VALUES ($1,$2::bigint[])
            ON CONFLICT (guild_id) DO UPDATE SET role_ids=$2::bigint[]
        """, inter.guild_id, role_ids)
    await inter.response.send_message(embed=success("Roles updated"), ephemeral=True)

@bot.tree.command(name="chcheck")
async def slash_chcheck(inter: discord.Interaction):
    async with bot.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT report_channel_id, role_ids FROM guild_settings WHERE guild_id=$1",
                                  inter.guild_id)
    if not row or not row["report_channel_id"]:
        return await inter.response.send_message(embed=error("No settings"), ephemeral=True)
    channel = inter.guild.get_channel(row["report_channel_id"])
    ch = channel.mention if channel else "Deleted"
    roles = " ".join(f"<@&{rid}>" for rid in row["role_ids"]) or "None"
    e = embed_base("Current Settings", 0x3498DB)
    e.add_field(name="Channel", value=ch, inline=False)
    e.add_field(name="Roles", value=roles, inline=False)
    await inter.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name="listinactive")
@commands.cooldown(1, 10, commands.BucketType.guild)
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
        return await inter.followup.send(embed=success("Everyone active today! 🎉"))
    view = MemberPages(inactive, "Inactive Today", 0xE74C3C)
    view.owner = inter.user.id
    msg = await inter.followup.send(embed=view.build(), view=view)
    view.msg = msg

@bot.tree.command(name="active")
@commands.cooldown(1, 10, commands.BucketType.guild)
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
        return await inter.followup.send(embed=success("No one active yet."))
    view = MemberPages(active, "Active Today", 0x2ECC71)
    view.owner = inter.user.id
    msg = await inter.followup.send(embed=view.build(), view=view)
    view.msg = msg

# ---------- TEXT PREFIX COPIES ----------
@bot.command(name="listinactive")
@commands.cooldown(1, 10, commands.BucketType.guild)
async def text_listinactive(ctx: commands.Context):
    today = datetime.date.today()
    async with bot.pool.acquire() as conn:
        active = {r["user_id"] for r in await conn.fetch("""
            SELECT user_id FROM user_activity
            WHERE guild_id=$1 AND last_active_date=$2
        """, ctx.guild.id, today)}
    inactive = [m for m in ctx.guild.members if not m.bot and m.id not in active]
    inactive.sort(key=lambda m: m.display_name.lower())
    if not inactive:
        return await ctx.send(embed=success("Everyone active today!"))
    view = MemberPages(inactive, "Inactive Today", 0xE74C3C)
    view.owner = ctx.author.id
    msg = await ctx.send(embed=view.build(), view=view)
    view.msg = msg

@bot.command(name="active")
@commands.cooldown(1, 10, commands.BucketType.guild)
async def text_active(ctx: commands.Context):
    today = datetime.date.today()
    async with bot.pool.acquire() as conn:
        active_ids = {r["user_id"] for r in await conn.fetch("""
            SELECT user_id FROM user_activity
            WHERE guild_id=$1 AND last_active_date=$2
        """, ctx.guild.id, today)}
    active = [m for m in ctx.guild.members if not m.bot and m.id in active_ids]
    active.sort(key=lambda m: m.display_name.lower())
    if not active:
        return await ctx.send(embed=success("No one active yet."))
    view = MemberPages(active, "Active Today", 0x2ECC71)
    view.owner = ctx.author.id
    msg = await ctx.send(embed=view.build(), view=view)
    view.msg = msg

# ---------- RUN ----------
bot.run(TOKEN)   # blocking – Render happy
