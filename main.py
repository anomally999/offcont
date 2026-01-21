# main.py
import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import asyncpg
import datetime
import os
from dotenv import load_dotenv
from typing import List, Optional, Dict
from aiohttp import web  # Already a discord.py dep—no extra install

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # Render provides this as env var

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class InactivityBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=",",
            intents=intents,
            help_command=None,
            description="Server activity tracker - tracks real message activity"
        )
        self.pool: Optional[asyncpg.Pool] = None

    async def setup_hook(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        await self.create_tables()
        await self.tree.sync()
        # Start the minimal web server in background
        self.bg_task = self.loop.create_task(self.webserver())

    async def webserver(self):
        async def handle(request):
            return web.Response(text="Bot is running")

        app = web.Application()
        app.add_routes([web.get('/', handle)])

        runner = web.AppRunner(app)
        await runner.setup()

        port = int(os.environ.get('PORT', 8080))  # Render provides PORT
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"Web server started on port {port}")

        # Keep the task alive forever (bot's main loop handles the rest)
        while True:
            await asyncio.sleep(3600)  # Sleep 1 hour—low overhead

    async def create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id BIGINT PRIMARY KEY,
                    report_channel_id BIGINT,
                    role_ids BIGINT[] DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS user_activity (
                    guild_id BIGINT,
                    user_id BIGINT,
                    last_active_date DATE,
                    offline_streak INT DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                );
            """)

    async def close(self):
        if self.pool:
            await self.pool.close()
        await super().close()

bot = InactivityBot()

# ────────────────────────────────────────────────
#              Beautiful Embed Factory
# ────────────────────────────────────────────────
def create_embed(title: str, color: int = 0x5865F2, description: str = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"✦ {title} ✦",
        color=color,
        timestamp=datetime.datetime.now(datetime.UTC),
        description=description
    )
    embed.set_footer(
        text="Activity Tracker • Real messages only",
        icon_url="https://i.imgur.com/awesomeclock.png"  # replace with real icon if you have one
    )
    embed.set_thumbnail(url=bot.user.avatar.url if bot.user.avatar else None)
    return embed

def error_embed(text: str) -> discord.Embed:
    return create_embed("Error", color=0xED4245, description=f"❌ {text}")

def success_embed(text: str) -> discord.Embed:
    return create_embed("Success", color=0x57F287, description=f"✅ {text}")

# ────────────────────────────────────────────────
#                   HELP COMMAND
# ────────────────────────────────────────────────
@bot.command(name="helpactivity")
async def text_help(ctx: commands.Context):
    embed = create_embed("Activity Tracker Commands", color=0xFEE75C)
    embed.description = (
        "Tracks **real message activity** per server.\n"
        "User = active only after sending ≥1 message that day.\n"
        "After **12 consecutive offline days** → report + ping\n\n"
        "**Commands:**\n"
    )
    embed.add_field(
        name=",helpactivity",
        value="Shows this message",
        inline=False
    )
    embed.add_field(
        name=",channelset #channel",
        value="Set report channel",
        inline=False
    )
    embed.add_field(
        name=",roleset @role1 @role2 ...",
        value="Roles to ping on report",
        inline=False
    )
    embed.add_field(
        name=",listinactive",
        value="Members who didn't message **today**",
        inline=False
    )
    embed.add_field(
        name="Slash commands",
        value="Same names without comma:\n`/channelset` `/roleset` `/listinactive`",
        inline=False
    )
    await ctx.send(embed=embed)

# ────────────────────────────────────────────────
#              EVENTS & ACTIVITY TRACKING
# ────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} • {datetime.datetime.now():%Y-%m-%d %H:%M}")
    check_inactivity_loop.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    guild_id = message.guild.id
    user_id = message.author.id
    today = datetime.date.today()

    async with bot.pool.acquire() as conn:
        # Upsert last active date
        await conn.execute("""
            INSERT INTO user_activity (guild_id, user_id, last_active_date, offline_streak)
            VALUES ($1, $2, $3, 0)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET
                last_active_date = EXCLUDED.last_active_date,
                offline_streak = CASE
                    WHEN user_activity.last_active_date = ($3 - INTERVAL '1 day')::date
                    THEN 0
                    ELSE user_activity.offline_streak + 1
                END
            WHERE user_activity.last_active_date != $3
        """, guild_id, user_id, today)

    await bot.process_commands(message)

# ────────────────────────────────────────────────
#              BACKGROUND CHECK (midnight-ish)
# ────────────────────────────────────────────────
@tasks.loop(hours=1)
async def check_inactivity_loop():
    if datetime.datetime.now(datetime.UTC).hour != 0:
        return

    today = datetime.date.today()

    async with bot.pool.acquire() as conn:
        guilds = await conn.fetch("SELECT guild_id, report_channel_id, role_ids FROM guild_settings WHERE report_channel_id IS NOT NULL")

        for record in guilds:
            guild_id, channel_id, role_ids = record
            guild = bot.get_guild(guild_id)
            if not guild:
                continue

            channel = guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                continue

            # Get users with high streak or inactive today
            inactive_long = await conn.fetch("""
                SELECT user_id, offline_streak
                FROM user_activity
                WHERE guild_id = $1
                  AND last_active_date < $2
                ORDER BY offline_streak DESC
            """, guild_id, today - datetime.timedelta(days=11))  # 12+ days means last < today-11

            if not inactive_long:
                continue

            roles = [guild.get_role(rid) for rid in role_ids if guild.get_role(rid)]
            ping = " ".join(r.mention for r in roles if r) or "@here"

            embed = create_embed("Long-term Inactive Members", color=0xED4245)
            embed.description = f"**12+ days** without any message • {today:%Y-%m-%d}"

            lines = []
            for row in inactive_long[:15]:  # limit to avoid huge messages
                member = guild.get_member(row["user_id"])
                name = member.display_name if member else f"<@{row['user_id']}> (left?)"
                lines.append(f"• {name}  —  **{row['offline_streak']}** days")

            embed.add_field(
                name=f"Offline ≥ 12 days ({len(inactive_long)} total)",
                value="\n".join(lines) or "None found",
                inline=False
            )

            if len(inactive_long) > 15:
                embed.add_field(name="Note", value=f"...and {len(inactive_long)-15} more", inline=False)

            await channel.send(ping, embed=embed)

# ────────────────────────────────────────────────
#                   SLASH COMMANDS
# ────────────────────────────────────────────────
@bot.tree.command(name="channelset", description="Set channel for long-inactivity reports")
@app_commands.describe(channel="Where to send the 12+ days reports")
async def slash_channelset(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(embed=error_embed("Manage Server permission required."), ephemeral=True)
        return

    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings (guild_id, report_channel_id)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET report_channel_id = $2
        """, interaction.guild_id, channel.id)

    await interaction.response.send_message(embed=success_embed(f"Report channel → {channel.mention}"), ephemeral=True)

@bot.tree.command(name="roleset", description="Roles to ping when sending long-inactivity list")
@app_commands.describe(roles="Mention roles here (multiple ok)")
async def slash_roleset(interaction: discord.Interaction, roles: str):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(embed=error_embed("Manage Server permission required."), ephemeral=True)
        return

    role_ids = [r.id for r in interaction.message.role_mentions] if interaction.message else []
    if not role_ids:
        await interaction.response.send_message(embed=error_embed("No roles detected. Mention them like @role"), ephemeral=True)
        return

    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings (guild_id, role_ids)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET role_ids = $2
        """, interaction.guild_id, role_ids)

    roles_mention = " ".join(f"<@&{rid}>" for rid in role_ids)
    await interaction.response.send_message(embed=success_embed(f"Ping roles updated:\n{roles_mention}"), ephemeral=True)

@bot.tree.command(name="listinactive", description="Show members who haven't messaged today")
async def slash_listinactive(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    today = datetime.date.today()
    async with bot.pool.acquire() as conn:
        records = await conn.fetch("""
            SELECT user_id
            FROM user_activity
            WHERE guild_id = $1 AND last_active_date != $2
            ORDER BY user_id
        """, interaction.guild_id, today)

    if not records:
        await interaction.followup.send(embed=success_embed("Everyone was active today! 🎉"), ephemeral=True)
        return

    members = [interaction.guild.get_member(r["user_id"]) for r in records]
    members = [m for m in members if m]  # filter None

    members.sort(key=lambda m: m.display_name.lower())

    embed = create_embed(f"Inactive Today ({len(members)})", color=0x5865F2)
    lines = [f"• {m.mention} ({m.display_name})" for m in members[:35]]
    embed.description = "\n".join(lines) + (f"\n\n...and {len(members)-35} more" if len(members)>35 else "")

    await interaction.followup.send(embed=embed, ephemeral=True)

# ────────────────────────────────────────────────
#               TEXT PREFIX VERSIONS
# ────────────────────────────────────────────────
@bot.command(name="channelset")
async def text_channelset(ctx: commands.Context, channel: discord.TextChannel):
    if not ctx.author.guild_permissions.manage_guild:
        return await ctx.send(embed=error_embed("Manage Server required."))

    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings (guild_id, report_channel_id)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET report_channel_id = $2
        """, ctx.guild.id, channel.id)

    await ctx.send(embed=success_embed(f"Report channel set: {channel.mention}"))

@bot.command(name="roleset")
async def text_roleset(ctx: commands.Context, *, roles_str: str):
    if not ctx.author.guild_permissions.manage_guild:
        return await ctx.send(embed=error_embed("Manage Server required."))

    role_mentions = ctx.message.role_mentions
    if not role_mentions:
        return await ctx.send(embed=error_embed("Mention roles like @role @role2"))

    role_ids = [r.id for r in role_mentions]

    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings (guild_id, role_ids)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET role_ids = $2
        """, ctx.guild.id, role_ids)

    await ctx.send(embed=success_embed(f"Roles updated: {' '.join(r.mention for r in role_mentions)}"))

@bot.command(name="listinactive")
async def text_listinactive(ctx: commands.Context):
    today = datetime.date.today()
    async with bot.pool.acquire() as conn:
        records = await conn.fetch("""
            SELECT user_id
            FROM user_activity
            WHERE guild_id = $1 AND last_active_date != $2
        """, ctx.guild.id, today)

    if not records:
        return await ctx.send(embed=success_embed("No one inactive today!"))

    members = [ctx.guild.get_member(r["user_id"]) for r in records if ctx.guild.get_member(r["user_id"])]
    embed = create_embed(f"Inactive Today • {len(members)}", color=0x3498DB)
    embed.description = "\n".join(f"• {m.mention}" for m in members[:30]) + \
                        (f"\n... +{len(members)-30} more" if len(members)>30 else "")
    await ctx.send(embed=embed)

# ────────────────────────────────────────────────
#                    RUN
# ────────────────────────────────────────────────
async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
