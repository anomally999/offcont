# main.py
import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import asyncpg
import datetime
import os
from dotenv import load_dotenv
from typing import List, Optional
from aiohttp import web

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

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
        self.bg_task = self.loop.create_task(self.webserver())

    async def webserver(self):
        async def handle(request):
            return web.Response(text="Bot is running")

        app = web.Application()
        app.add_routes([web.get('/', handle)])

        runner = web.AppRunner(app)
        await runner.setup()

        port = int(os.environ.get('PORT', 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"Web server listening on port {port}")

        while True:
            await asyncio.sleep(3600)

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
#              Embed Helpers
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
        icon_url=bot.user.avatar.url if bot.user and bot.user.avatar else None
    )
    return embed

def error_embed(text: str) -> discord.Embed:
    return create_embed("Error", 0xED4245, f"❌ {text}")

def success_embed(text: str) -> discord.Embed:
    return create_embed("Success", 0x57F287, f"✅ {text}")

# ────────────────────────────────────────────────
#              HELP COMMAND
# ────────────────────────────────────────────────
@bot.command(name="helpactivity")
async def text_help(ctx: commands.Context):
    embed = create_embed("Activity Tracker Commands", 0xFEE75C)
    embed.description = (
        "Tracks **real message activity** in this server.\n"
        "→ Active = sent at least 1 message today\n"
        "→ After **12 consecutive offline days** → automatic alert + ping\n\n"
        "**Available commands:**"
    )
    embed.add_field(name=",helpactivity", value="This message", inline=False)
    embed.add_field(name=",channelset #channel", value="Set alert channel", inline=False)
    embed.add_field(name=",roleset @role @role...", value="Roles to ping on alerts", inline=False)
    embed.add_field(name=",chcheck", value="Show current settings", inline=False)
    embed.add_field(name=",listinactive", value="List members inactive today", inline=False)
    embed.add_field(name="Slash version", value="Use /channelset /roleset /chcheck /listinactive", inline=False)
    await ctx.send(embed=embed)

# ────────────────────────────────────────────────
#              EVENTS
# ────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} • {datetime.datetime.now():%Y-%m-%d %H:%M}")
    check_inactivity_loop.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    gid = message.guild.id
    uid = message.author.id
    today = datetime.date.today()

    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO user_activity (guild_id, user_id, last_active_date, offline_streak)
            VALUES ($1, $2, $3, 0)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET
                last_active_date = EXCLUDED.last_active_date,
                offline_streak = CASE
                    WHEN user_activity.last_active_date = ($3 - INTERVAL '1 day')::date THEN 0
                    ELSE user_activity.offline_streak + 1
                END
            WHERE user_activity.last_active_date != $3
        """, gid, uid, today)

    await bot.process_commands(message)

# ────────────────────────────────────────────────
#              AUTO ALERT (12+ days)
# ────────────────────────────────────────────────
@tasks.loop(hours=1)
async def check_inactivity_loop():
    if datetime.datetime.now(datetime.UTC).hour != 0:
        return

    today = datetime.date.today()

    async with bot.pool.acquire() as conn:
        guilds = await conn.fetch("""
            SELECT guild_id, report_channel_id, role_ids
            FROM guild_settings
            WHERE report_channel_id IS NOT NULL
        """)

        for rec in guilds:
            gid, chid, rids = rec
            guild = bot.get_guild(gid)
            if not guild:
                continue

            channel = guild.get_channel(chid)
            if not channel or not isinstance(channel, discord.TextChannel):
                continue

            inactive = await conn.fetch("""
                SELECT user_id,
                       offline_streak + (CURRENT_DATE - last_active_date) AS current_streak
                FROM user_activity
                WHERE guild_id = $1
                  AND last_active_date <= CURRENT_DATE - INTERVAL '12 days'
                ORDER BY current_streak DESC
            """, gid)

            if not inactive:
                continue

            roles = [guild.get_role(rid) for rid in rids if guild.get_role(rid)]
            ping = " ".join(r.mention for r in roles if r) or "@here"

            embed = create_embed("Long-term Inactive Members", 0xED4245)
            embed.description = f"**12+ consecutive days** offline • {today:%Y-%m-%d}"

            lines = []
            for row in inactive[:12]:
                member = guild.get_member(row["user_id"])
                if not member:
                    continue
                name = member.mention if member else f"<@{row['user_id']}>"
                lines.append(f"• {name} — **{row['current_streak']}** days")

            embed.add_field(
                name=f"Members ({len(inactive)} total)",
                value="\n".join(lines) or "None",
                inline=False
            )

            if len(inactive) > 12:
                embed.add_field(name="Note", value=f"...and {len(inactive)-12} more", inline=False)

            await channel.send(ping, embed=embed)

# ────────────────────────────────────────────────
#              PAGINATION FOR LISTINACTIVE
# ────────────────────────────────────────────────
class InactivePagination(discord.ui.View):
    def __init__(self, members: List[discord.Member], per_page: int = 15):
        super().__init__(timeout=600)
        self.members = members
        self.per_page = per_page
        self.page = 0
        self.max_page = (len(members) + per_page - 1) // per_page - 1

        self.previous.disabled = True
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= self.max_page

    def get_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        end = start + self.per_page
        page_members = self.members[start:end]

        embed = create_embed(
            f"Members Inactive Today ({len(self.members)} total)",
            0x5865F2,
            f"Page {self.page+1}/{self.max_page+1}"
        )

        embed.description = "\n".join(
            f"• {m.mention} ({m.display_name})" for m in page_members
        ) or "No one on this page"

        return embed

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.grey)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.grey)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

# ────────────────────────────────────────────────
#              SLASH COMMANDS
# ────────────────────────────────────────────────
@bot.tree.command(name="channelset", description="Set the channel for inactivity alerts")
@app_commands.describe(channel="Channel where alerts will be sent")
async def slash_channelset(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message(embed=error_embed("Requires Manage Server permission"), ephemeral=True)

    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings (guild_id, report_channel_id)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET report_channel_id = $2
        """, interaction.guild_id, channel.id)

    await interaction.response.send_message(embed=success_embed(f"Alert channel set to {channel.mention}"), ephemeral=True)

@bot.tree.command(name="roleset", description="Set roles to ping on inactivity alerts (up to 5)")
@app_commands.describe(
    role1="Required role to ping",
    role2="Optional second role",
    role3="Optional third role",
    role4="Optional fourth role",
    role5="Optional fifth role"
)
async def slash_roleset(
    interaction: discord.Interaction,
    role1: discord.Role,
    role2: Optional[discord.Role] = None,
    role3: Optional[discord.Role] = None,
    role4: Optional[discord.Role] = None,
    role5: Optional[discord.Role] = None
):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message(embed=error_embed("Requires Manage Server permission"), ephemeral=True)

    roles = [r for r in [role1, role2, role3, role4, role5] if r]
    if not roles:
        return await interaction.response.send_message(embed=error_embed("Select at least one role"), ephemeral=True)

    role_ids = [r.id for r in roles]

    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings (guild_id, role_ids)
            VALUES ($1, $2::bigint[])
            ON CONFLICT (guild_id) DO UPDATE SET role_ids = $2::bigint[]
        """, interaction.guild_id, role_ids)

    mentions = " ".join(r.mention for r in roles)
    await interaction.response.send_message(embed=success_embed(f"Roles set:\n{mentions}"), ephemeral=True)

@bot.tree.command(name="chcheck", description="View current alert settings")
async def slash_chcheck(interaction: discord.Interaction):
    async with bot.pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT report_channel_id, role_ids
            FROM guild_settings WHERE guild_id = $1
        """, interaction.guild_id)

    if not row or not row["report_channel_id"]:
        return await interaction.response.send_message(embed=error_embed("No settings configured yet"), ephemeral=True)

    channel = interaction.guild.get_channel(row["report_channel_id"])
    ch_mention = channel.mention if channel else f"Deleted channel (ID: {row['report_channel_id']})"

    roles_str = " ".join(f"<@&{rid}>" for rid in row["role_ids"]) or "No roles set"

    embed = create_embed("Current Alert Settings", 0x3498DB)
    embed.add_field(name="Alert Channel", value=ch_mention, inline=False)
    embed.add_field(name="Ping Roles", value=roles_str, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="listinactive", description="Show who hasn't messaged today (paginated)")
async def slash_listinactive(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    guild = interaction.guild
    today = datetime.date.today()

    async with bot.pool.acquire() as conn:
        active = await conn.fetch("""
            SELECT user_id FROM user_activity
            WHERE guild_id = $1 AND last_active_date = $2
        """, guild.id, today)

    active_ids = {r["user_id"] for r in active}

    inactive = [m for m in guild.members if not m.bot and m.id not in active_ids]
    inactive.sort(key=lambda m: m.display_name.lower())

    if not inactive:
        return await interaction.followup.send(embed=success_embed("Everyone has been active today! 🎉"))

    view = InactivePagination(inactive)
    await interaction.followup.send(embed=view.get_embed(), view=view)

# ────────────────────────────────────────────────
#              TEXT PREFIX COMMANDS
# ────────────────────────────────────────────────
@bot.command(name="channelset")
async def text_channelset(ctx: commands.Context, channel: discord.TextChannel):
    if not ctx.author.guild_permissions.manage_guild:
        return await ctx.send(embed=error_embed("Manage Server required"))

    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings (guild_id, report_channel_id)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET report_channel_id = $2
        """, ctx.guild.id, channel.id)

    await ctx.send(embed=success_embed(f"Alert channel → {channel.mention}"))

@bot.command(name="roleset")
async def text_roleset(ctx: commands.Context, *, content: str = ""):
    if not ctx.author.guild_permissions.manage_guild:
        return await ctx.send(embed=error_embed("Manage Server required"))

    roles = ctx.message.role_mentions
    if not roles:
        return await ctx.send(embed=error_embed("Mention at least one role\nExample: ,roleset @Staff @Moderators"))

    role_ids = [r.id for r in roles]

    async with bot.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO guild_settings (guild_id, role_ids)
            VALUES ($1, $2::bigint[])
            ON CONFLICT (guild_id) DO UPDATE SET role_ids = $2::bigint[]
        """, ctx.guild.id, role_ids)

    await ctx.send(embed=success_embed(f"Roles set: {' '.join(r.mention for r in roles)}"))

@bot.command(name="chcheck")
async def text_chcheck(ctx: commands.Context):
    async with bot.pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT report_channel_id, role_ids FROM guild_settings
            WHERE guild_id = $1
        """, ctx.guild.id)

    if not row or not row["report_channel_id"]:
        return await ctx.send(embed=error_embed("No settings configured yet"))

    channel = ctx.guild.get_channel(row["report_channel_id"])
    ch_text = channel.mention if channel else f"Deleted (ID: {row['report_channel_id']})"

    roles_text = " ".join(f"<@&{rid}>" for rid in row["role_ids"]) or "None"

    embed = create_embed("Current Settings", 0x3498DB)
    embed.add_field(name="Alert Channel", value=ch_text, inline=False)
    embed.add_field(name="Ping Roles", value=roles_text, inline=False)

    await ctx.send(embed=embed)

@bot.command(name="listinactive")
async def text_listinactive(ctx: commands.Context):
    guild = ctx.guild
    today = datetime.date.today()

    async with bot.pool.acquire() as conn:
        active = await conn.fetch("""
            SELECT user_id FROM user_activity
            WHERE guild_id = $1 AND last_active_date = $2
        """, guild.id, today)

    active_ids = {r["user_id"] for r in active}

    inactive = [m for m in guild.members if not m.bot and m.id not in active_ids]
    inactive.sort(key=lambda m: m.display_name.lower())

    if not inactive:
        return await ctx.send(embed=success_embed("Everyone has been active today!"))

    view = InactivePagination(inactive)
    await ctx.send(embed=view.get_embed(), view=view)

# ────────────────────────────────────────────────
#              RUN
# ────────────────────────────────────────────────
async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
