"""
Accountant — a PNL tracker Discord bot.
Log profits/losses, check balances, battle for the leaderboard.

Features:
- /profit, /spend, /balance, /undo, /reset
- /leaderboard (all-time / daily / weekly / monthly)
- /graph — line graph of cumulative PNL over time
- Hourly auto-posted leaderboard in a set channel
- Overtake pings when someone passes someone else
- Auto-assigns a special role to whoever is #1

Setup:
1. pip install discord.py matplotlib
2. Set your bot token: set DISCORD_TOKEN=your_token   (Windows)
3. python accountant_bot.py
4. In Discord (admin only):
   /setleaderboardchannel  — where the hourly leaderboard goes
   /setalertchannel        — where overtake pings go
   /settoprole             — role given to #1 (paste role ID)

Bot needs "Manage Roles" permission for the top role feature,
and its role must be ABOVE the top role in the role list.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import sqlite3
import os
import io
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ── Config ──────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_TOKEN_HERE")
DB_PATH = os.getenv("DB_PATH", "pnl.db")  # on Railway, set DB_PATH=/data/pnl.db with a volume at /data

# ── Database ────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            guild_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (guild_id, key)
        )
    """)
    conn.commit()
    return conn

def set_config(guild_id: int, key: str, value: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
        (guild_id, key, value),
    )
    conn.commit()
    conn.close()

def get_config(guild_id: int, key: str):
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM config WHERE guild_id = ? AND key = ?", (guild_id, key)
    ).fetchone()
    conn.close()
    return row[0] if row else None

def get_standings():
    """Returns list of (user_id, total) sorted best-first."""
    conn = get_db()
    rows = conn.execute(
        "SELECT user_id, SUM(amount) as total FROM entries "
        "GROUP BY user_id ORDER BY total DESC"
    ).fetchall()
    conn.close()
    return rows

def get_total(user_id: int):
    conn = get_db()
    total = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM entries WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    conn.close()
    return total

# ── Bot setup ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True  # needed for role management + names
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    get_db()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Sync error: {e}")
    if not hourly_leaderboard.is_running():
        hourly_leaderboard.start()
    print(f"Accountant is online as {bot.user}")

# ── Leaderboard embed builder ──────────────────────────────────────────────────
def build_leaderboard_embed(guild: discord.Guild, rows, title="🏆 All-Time Leaderboard"):
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, total) in enumerate(rows[:15]):
        prefix = medals[i] if i < 3 else f"`{i+1}.`"
        member = guild.get_member(uid) if guild else None
        name = member.display_name if member else f"User {uid}"
        sign = "+" if total >= 0 else ""
        lines.append(f"{prefix} **{name}** — `{sign}${total:,.2f}`")
    embed = discord.Embed(
        title=title,
        description="\n".join(lines) if lines else "No entries yet!",
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"{len(rows)} traders tracked • Accountant")
    embed.timestamp = datetime.now(timezone.utc)
    return embed

# ── Graph builder ──────────────────────────────────────────────────────────────
def build_pnl_graph(guild: discord.Guild, user_ids):
    """Returns a BytesIO PNG of cumulative PNL lines, or None if no data."""
    conn = get_db()
    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("#2b2d31")
    ax.set_facecolor("#2b2d31")

    plotted = False
    for uid in user_ids:
        rows = conn.execute(
            "SELECT created_at, amount FROM entries WHERE user_id = ? ORDER BY id",
            (uid,),
        ).fetchall()
        if not rows:
            continue
        times, cumulative = [], []
        running = 0.0
        for ts, amt in rows:
            running += amt
            times.append(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"))
            cumulative.append(running)
        member = guild.get_member(uid) if guild else None
        name = member.display_name if member else f"User {uid}"
        ax.plot(times, cumulative, marker="o", markersize=3, linewidth=2, label=name)
        plotted = True
    conn.close()

    if not plotted:
        plt.close(fig)
        return None

    ax.axhline(0, color="#555", linewidth=0.8)
    ax.set_title("Cumulative PNL Over Time", color="white", fontsize=14)
    ax.set_ylabel("PNL ($)", color="white")
    ax.tick_params(colors="white")
    for spine in ax.spines.values():
        spine.set_color("#555")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.legend(facecolor="#2b2d31", labelcolor="white", edgecolor="#555")
    ax.grid(True, alpha=0.15)
    fig.autofmt_xdate()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
    buf.seek(0)
    plt.close(fig)
    return buf

# ── Live leaderboard (one message, always kept up to date) ─────────────────────
async def update_live_leaderboard(guild: discord.Guild):
    """Edit (or create) the single live leaderboard message with embed + graph."""
    if guild is None:
        return
    channel_id = get_config(guild.id, "leaderboard_channel")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if not channel:
        return

    rows = get_standings()
    embed = build_leaderboard_embed(guild, rows, "🏆 Live PNL Leaderboard")

    files = []
    buf = build_pnl_graph(guild, [uid for uid, _ in rows[:10]])
    if buf:
        files = [discord.File(buf, filename="leaderboard.png")]
        embed.set_image(url="attachment://leaderboard.png")

    msg_id = get_config(guild.id, "live_message")
    try:
        if msg_id:
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(embed=embed, attachments=files)
            return
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass  # fall through and send a fresh one

    try:
        msg = await channel.send(embed=embed, files=files)
        set_config(guild.id, "live_message", str(msg.id))
    except discord.Forbidden:
        pass

# ── Hourly refresher (safety net) ──────────────────────────────────────────────
@tasks.loop(hours=1)
async def hourly_leaderboard():
    for guild in bot.guilds:
        await update_live_leaderboard(guild)
        await enforce_top_role(guild)

@hourly_leaderboard.before_loop
async def before_hourly():
    await bot.wait_until_ready()

# ── Overtake detection + top role ──────────────────────────────────────────────
async def handle_standings_change(guild: discord.Guild, before, after, actor_id: int):
    """Compare standings before/after an entry; send overtake pings + update top role."""
    if guild is None:
        return

    # Overtake pings: the actor moved up past anyone previously above them
    before_rank = {uid: i for i, (uid, _) in enumerate(before)}
    after_rank = {uid: i for i, (uid, _) in enumerate(after)}

    overtaken = []
    if actor_id in before_rank and actor_id in after_rank:
        if after_rank[actor_id] < before_rank[actor_id]:
            for uid, _ in before:
                if uid == actor_id:
                    continue
                # previously above actor, now below
                if before_rank[uid] < before_rank[actor_id] and after_rank.get(uid, 999) > after_rank[actor_id]:
                    overtaken.append(uid)
    elif actor_id not in before_rank and actor_id in after_rank:
        # first ever entry — they overtake everyone now below them
        overtaken = [uid for uid, _ in after if after_rank[uid] > after_rank[actor_id]]

    alert_channel_id = get_config(guild.id, "alert_channel")
    if overtaken and alert_channel_id:
        channel = guild.get_channel(int(alert_channel_id))
        if channel:
            for uid in overtaken:
                try:
                    await channel.send(
                        f"@everyone 🚨 <@{actor_id}> overtakes <@{uid}> on the PNL leaderboard!"
                    )
                except discord.Forbidden:
                    break

    # Top role: make sure only current #1 has it
    await enforce_top_role(guild, after)

    # Keep the live leaderboard fresh
    await update_live_leaderboard(guild)

async def enforce_top_role(guild: discord.Guild, standings=None):
    """Give the configured role to #1 and remove it from everyone else."""
    if standings is None:
        standings = get_standings()
    role_id = get_config(guild.id, "top_role")
    if not role_id:
        return
    role = guild.get_role(int(role_id))
    if not role:
        return
    if not standings:
        # nobody has entries — nobody should have the role
        for member in role.members:
            try:
                await member.remove_roles(role, reason="Leaderboard is empty")
            except discord.Forbidden:
                pass
        return
    top_uid = standings[0][0]
    for member in role.members:
        if member.id != top_uid:
            try:
                await member.remove_roles(role, reason="Lost #1 PNL spot")
            except discord.Forbidden:
                pass
    top_member = guild.get_member(top_uid)
    if top_member and role not in top_member.roles:
        try:
            await top_member.add_roles(role, reason="Reached #1 PNL spot")
        except discord.Forbidden:
            pass

# ── Entry logging core (shared by /profit and /spend) ──────────────────────────
async def log_entry(interaction: discord.Interaction, amount: float, note: str, title: str):
    before = get_standings()

    conn = get_db()
    conn.execute(
        "INSERT INTO entries (user_id, amount, note) VALUES (?, ?, ?)",
        (interaction.user.id, amount, note),
    )
    conn.commit()
    conn.close()

    after = get_standings()
    total = get_total(interaction.user.id)

    sign = "+" if amount >= 0 else ""
    color = discord.Color.green() if amount >= 0 else discord.Color.red()
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Amount", value=f"`{sign}${amount:,.2f}`", inline=True)
    embed.add_field(name="New Total", value=f"`${total:,.2f}`", inline=True)
    if note:
        embed.add_field(name="Note", value=note, inline=False)
    embed.set_footer(text=f"Logged by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

    await handle_standings_change(interaction.guild, before, after, interaction.user.id)

# ── /profit ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="profit", description="Log money you made")
@app_commands.describe(amount="Amount in $ you made", note="Optional note")
async def profit(interaction: discord.Interaction, amount: float, note: str = None):
    await log_entry(interaction, abs(amount), note, "📒 Profit Logged")

# ── /spend ──────────────────────────────────────────────────────────────────────
@bot.tree.command(name="spend", description="Log money you spent (subtracted from your total)")
@app_commands.describe(amount="Amount in $ you spent", note="Optional note (e.g. what you spent it on)")
async def spend(interaction: discord.Interaction, amount: float, note: str = None):
    await log_entry(interaction, -abs(amount), note, "💸 Expense Logged")

# ── /balance ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="balance", description="Check your (or someone else's) total PNL")
@app_commands.describe(user="User to check (defaults to you)")
async def balance(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    total = get_total(target.id)

    conn = get_db()
    rows = conn.execute(
        "SELECT amount, note FROM entries WHERE user_id = ? ORDER BY id DESC LIMIT 5",
        (target.id,),
    ).fetchall()
    conn.close()

    color = discord.Color.green() if total >= 0 else discord.Color.red()
    embed = discord.Embed(title=f"💰 {target.display_name}'s PNL", color=color)
    embed.add_field(name="Total", value=f"`${total:,.2f}`", inline=False)

    if rows:
        history = []
        for amt, nt in rows:
            sign = "+" if amt >= 0 else ""
            line = f"`{sign}${amt:,.2f}`"
            if nt:
                line += f" — {nt}"
            history.append(line)
        embed.add_field(name="Recent Entries", value="\n".join(history), inline=False)

    await interaction.response.send_message(embed=embed)

# ── /leaderboard ────────────────────────────────────────────────────────────────
@bot.tree.command(name="leaderboard", description="See who's making the most money")
@app_commands.describe(period="Time period to filter by")
@app_commands.choices(period=[
    app_commands.Choice(name="All Time", value="all"),
    app_commands.Choice(name="Today", value="daily"),
    app_commands.Choice(name="This Week", value="weekly"),
    app_commands.Choice(name="This Month", value="monthly"),
])
async def leaderboard(interaction: discord.Interaction, period: str = "all"):
    now = datetime.now(timezone.utc)
    if period == "daily":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        title = "🏆 Today's Leaderboard"
    elif period == "weekly":
        cutoff = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        title = "🏆 This Week's Leaderboard"
    elif period == "monthly":
        cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        title = "🏆 This Month's Leaderboard"
    else:
        cutoff = None
        title = "🏆 All-Time Leaderboard"

    conn = get_db()
    if cutoff:
        rows = conn.execute(
            "SELECT user_id, SUM(amount) as total FROM entries "
            "WHERE created_at >= ? GROUP BY user_id ORDER BY total DESC",
            (cutoff.strftime("%Y-%m-%d %H:%M:%S"),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT user_id, SUM(amount) as total FROM entries "
            "GROUP BY user_id ORDER BY total DESC"
        ).fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("No entries yet! Use `/profit` to log your first one.")
        return

    await interaction.response.send_message(embed=build_leaderboard_embed(interaction.guild, rows, title))

# ── /graph ──────────────────────────────────────────────────────────────────────
@bot.tree.command(name="graph", description="Line graph of cumulative PNL over time")
@app_commands.describe(user="Graph just one user (default: top 10 traders)")
async def graph(interaction: discord.Interaction, user: discord.User = None):
    await interaction.response.defer()

    if user:
        user_ids = [user.id]
    else:
        user_ids = [uid for uid, _ in get_standings()[:10]]

    buf = build_pnl_graph(interaction.guild, user_ids)
    if not buf:
        await interaction.followup.send("No data to graph yet!")
        return

    await interaction.followup.send(file=discord.File(buf, filename="pnl_graph.png"))

# ── /undo ───────────────────────────────────────────────────────────────────────
@bot.tree.command(name="undo", description="Undo your last entry")
async def undo(interaction: discord.Interaction):
    conn = get_db()
    row = conn.execute(
        "SELECT id, amount, note FROM entries WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (interaction.user.id,),
    ).fetchone()
    if not row:
        conn.close()
        await interaction.response.send_message("Nothing to undo!", ephemeral=True)
        return
    conn.execute("DELETE FROM entries WHERE id = ?", (row[0],))
    conn.commit()
    conn.close()
    sign = "+" if row[1] >= 0 else ""
    await interaction.response.send_message(
        f"Removed `{sign}${row[1]:,.2f}`" + (f" ({row[2]})" if row[2] else ""),
        ephemeral=True,
    )
    await enforce_top_role(interaction.guild)
    await update_live_leaderboard(interaction.guild)

# ── /reset ──────────────────────────────────────────────────────────────────────
@bot.tree.command(name="reset", description="Reset your PNL to zero (deletes all your entries)")
async def reset(interaction: discord.Interaction):
    conn = get_db()
    cur = conn.execute("DELETE FROM entries WHERE user_id = ?", (interaction.user.id,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    await interaction.response.send_message(
        f"Cleared **{deleted}** entries. Starting fresh!", ephemeral=True
    )
    await enforce_top_role(interaction.guild)
    await update_live_leaderboard(interaction.guild)

# ── Admin setup commands ────────────────────────────────────────────────────────
@bot.tree.command(name="setleaderboardchannel", description="[Admin] Set channel for hourly leaderboard posts")
@app_commands.describe(channel="Channel for hourly updates")
@app_commands.default_permissions(administrator=True)
async def setleaderboardchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    set_config(interaction.guild.id, "leaderboard_channel", str(channel.id))
    set_config(interaction.guild.id, "live_message", "")  # force a fresh message
    await interaction.response.send_message(
        f"Live leaderboard will be kept up to date in {channel.mention}", ephemeral=True
    )
    await update_live_leaderboard(interaction.guild)

@bot.tree.command(name="setalertchannel", description="[Admin] Set channel for overtake pings")
@app_commands.describe(channel="Channel for overtake alerts")
@app_commands.default_permissions(administrator=True)
async def setalertchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    set_config(interaction.guild.id, "alert_channel", str(channel.id))
    await interaction.response.send_message(
        f"Overtake alerts will be sent in {channel.mention}", ephemeral=True
    )

@bot.tree.command(name="settoprole", description="[Admin] Set the role given to #1 on the leaderboard")
@app_commands.describe(role_id="The role ID (right-click role → Copy ID)")
@app_commands.default_permissions(administrator=True)
async def settoprole(interaction: discord.Interaction, role_id: str):
    if not role_id.isdigit() or not interaction.guild.get_role(int(role_id)):
        await interaction.response.send_message("Invalid role ID for this server.", ephemeral=True)
        return
    set_config(interaction.guild.id, "top_role", role_id)
    role = interaction.guild.get_role(int(role_id))
    await interaction.response.send_message(
        f"#1 on the leaderboard will now get the **{role.name}** role", ephemeral=True
    )
    await enforce_top_role(interaction.guild)  # apply immediately

# ── Run ─────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(TOKEN)
