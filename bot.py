import discord
from discord.ext import tasks, commands
import json
import os
import re
import asyncio
import signal
import sys
import zipfile
import io
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional, Dict, List, Tuple
import psycopg2
import psycopg2.pool
import psycopg2.extras

# --- Configuration ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    case_insensitive=True
)

DATA_FILE = 'data.json'

# --- Permission Configuration ---
OWNER_USERNAME = "evanora0"
ALLOWED_ROLES = {"dev", "moderator", "mod", "admin"}

# --- Anti-Spam Config ---
KILL_COOLDOWN = timedelta(minutes=5)  # Reject duplicate kills within 5 minutes

# --- In-Memory Cache (boss static data only) ---
SPAWN_DATA: Optional[Dict] = None
BOSS_LOOKUP: Dict[str, dict] = {}
BOSS_NAME_LOOKUP: Dict[str, dict] = {}
CATEGORY_LOOKUP: Dict[str, str] = {}

# Last 200 log entries kept in memory for fast display
logs_cache: List[str] = []

# --- Database Connection Pool ---
db_pool: Optional[psycopg2.pool.SimpleConnectionPool] = None

# --- Boss Shortcuts ---
BOSS_SHORTCUTS = {
    "mani": "Crom's Hellborne Manikin",
    "prot": "Proteus", "gele": "Gelebron", "bt": "Bloodthorn the Ravenous",
    "bloodthorn": "Bloodthorn the Ravenous", "dino": "Dhiothu",
    "necro": "Efnisien the Necromancer", "mord": "Mordris", "hrung": "Hrungnir",
    "180": "King Snorri Bonechewer", "70": "Inferno Fellfire",
    "75": "Sleatskean Chillmist", "80": "Aberrant Starspell",
    "85": "Spirehoof the Corrupted", "falgren": "Falgren Bloodbinde",
    "fal": "Falgren Bloodbinde", "90": "Falgren Bloodbinde",
    "doggy85": "Stonefang", "stone": "Stonefang", "fang": "Stonefang",
    "gor": "Goretusk", "pig": "Goretusk", "bone": "Bonehead",
    "90bo": "Bonehead", "90bl": "Bladewing", "rb": "Redbane",
    "95red": "Redbane", "100sp": "Spearhorn", "sp": "Spearhorn",
    "spear": "Spearhorn", "95": "Ironscale", "iron": "Ironscale",
    "100": "Shivercowl", "100sh": "Shivercowl", "rock": "Rockbelly",
    "105lir": "Rockbelly", "cop": "Coppinger", "120": "The All-Knowing One",
    "eye": "The All-Knowing One", "125": "The Swamp King",
    "swapy": "The Swamp King", "130": "Gnarlroot the Ancient",
    "woody": "Gnarlroot the Ancient", "135": "Chained Empero",
    "chain": "Chained Empero", "stonelord": "Ragnok the Stonelord",
    "140": "Ragnok the Stonelord", "145": "Ignus the Lavalord",
    "lavalord": "Ignus the Lavalord", "150": "Glashtyn Deepscale",
    "spider": "Spider Queen Ulrob", "155": "Spider Queen Ulrob",
    "160": "High Priest Bor-Ag-Valon", "165": "High King Krem-Nor-Borok",
    "170": "Firbolg Champion Sreng", "185": "Ifryn Onyxclaw",
    "190": "Magister Skath", "195": "General Gron", "200": "Krothur the Condemned",
    "205": "Cragskor", "210": "Revenant of Anguish", "215": "Unox Mindrender"
}

CATEGORY_COMMANDS = {
    "warden": {"category": "Warden Bosses", "aliases": ["wardens"]},
    "meteoric": {"category": "Meteoric Bosses", "aliases": ["meteors"]},
    "frozen": {"category": "Frozen Bosses", "aliases": ["frozens"]},
    "dl": {"category": "Dragonlord Bosses", "aliases": ["dragonlord", "dragonlords"]},
    "edl": {"category": "Exalted Dragonlord Bosses", "aliases": ["exalted", "exalteddragonlord"]},
    "midr": {"category": "Mid Raids", "aliases": ["midraids", "mid"]},
    "raids": {"category": "Endgame Raids", "aliases": ["endraids", "endgame"]}
}

RAID_CATEGORIES = {"Mid Raids", "Endgame Raids"}

# ===========================
#     DATABASE HELPERS
# ===========================

def get_conn():
    """Borrow a connection from the pool."""
    return db_pool.getconn()

def put_conn(conn):
    """Return a connection to the pool."""
    db_pool.putconn(conn)

def init_db():
    """Create tables if they don't exist and initialise the connection pool."""
    global db_pool
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        host     = os.getenv("PGHOST", "localhost")
        port     = os.getenv("PGPORT", "5432")
        user     = os.getenv("PGUSER", "postgres")
        password = os.getenv("PGPASSWORD", "")
        dbname   = os.getenv("PGDATABASE", "postgres")
        dsn = f"host={host} port={port} user={user} password={password} dbname={dbname}"

    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS timers (
                    guild_id  TEXT NOT NULL,
                    boss_key  TEXT NOT NULL,
                    last_kill TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (guild_id, boss_key)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    guild_id   TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notified (
                    notification_key TEXT PRIMARY KEY,
                    notified         BOOLEAN NOT NULL DEFAULT TRUE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS permissions (
                    guild_id TEXT NOT NULL,
                    user_id  TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id        SERIAL PRIMARY KEY,
                    message   TEXT NOT NULL,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        conn.commit()
        print("✅ Database schema ready")
    finally:
        put_conn(conn)

# ===========================
#   DB QUERY HELPERS (sync)
# ===========================

def db_get_timer(guild_id: str, boss_key: str) -> Optional[datetime]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_kill FROM timers WHERE guild_id=%s AND boss_key=%s",
                (guild_id, boss_key)
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        put_conn(conn)

def db_get_guild_timers(guild_id: str) -> Dict[str, datetime]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT boss_key, last_kill FROM timers WHERE guild_id=%s",
                (guild_id,)
            )
            return {row[0]: row[1] for row in cur.fetchall()}
    finally:
        put_conn(conn)

def db_get_all_timers() -> Dict[str, Dict[str, datetime]]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT guild_id, boss_key, last_kill FROM timers")
            result: Dict[str, Dict[str, datetime]] = defaultdict(dict)
            for guild_id, boss_key, last_kill in cur.fetchall():
                result[guild_id][boss_key] = last_kill
            return result
    finally:
        put_conn(conn)

def db_upsert_timer(guild_id: str, boss_key: str, last_kill: datetime):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO timers (guild_id, boss_key, last_kill)
                VALUES (%s, %s, %s)
                ON CONFLICT (guild_id, boss_key) DO UPDATE SET last_kill = EXCLUDED.last_kill
                """,
                (guild_id, boss_key, last_kill)
            )
        conn.commit()
    finally:
        put_conn(conn)

def db_delete_timer(guild_id: str, boss_key: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM timers WHERE guild_id=%s AND boss_key=%s",
                (guild_id, boss_key)
            )
        conn.commit()
    finally:
        put_conn(conn)

def db_delete_guild_timers(guild_id: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM timers WHERE guild_id=%s", (guild_id,))
        conn.commit()
    finally:
        put_conn(conn)

def db_get_channel(guild_id: str) -> Optional[str]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT channel_id FROM channels WHERE guild_id=%s", (guild_id,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        put_conn(conn)

def db_get_all_channels() -> Dict[str, str]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT guild_id, channel_id FROM channels")
            return {row[0]: row[1] for row in cur.fetchall()}
    finally:
        put_conn(conn)

def db_upsert_channel(guild_id: str, channel_id: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO channels (guild_id, channel_id)
                VALUES (%s, %s)
                ON CONFLICT (guild_id) DO UPDATE SET channel_id = EXCLUDED.channel_id
                """,
                (guild_id, channel_id)
            )
        conn.commit()
    finally:
        put_conn(conn)

def db_is_notified(key: str) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT notified FROM notified WHERE notification_key=%s", (key,))
            row = cur.fetchone()
            return bool(row[0]) if row else False
    finally:
        put_conn(conn)

def db_set_notified(key: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notified (notification_key, notified)
                VALUES (%s, TRUE)
                ON CONFLICT (notification_key) DO NOTHING
                """,
                (key,)
            )
        conn.commit()
    finally:
        put_conn(conn)

def db_delete_notified(key: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM notified WHERE notification_key=%s", (key,))
        conn.commit()
    finally:
        put_conn(conn)

def db_delete_notified_prefix(prefix: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM notified WHERE notification_key LIKE %s",
                (prefix + "%",)
            )
        conn.commit()
    finally:
        put_conn(conn)

def db_is_permitted(guild_id: str, user_id: str) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM permissions WHERE guild_id=%s AND user_id=%s",
                (guild_id, user_id)
            )
            return cur.fetchone() is not None
    finally:
        put_conn(conn)

def db_get_permitted_users(guild_id: str) -> List[str]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM permissions WHERE guild_id=%s", (guild_id,))
            return [row[0] for row in cur.fetchall()]
    finally:
        put_conn(conn)

def db_add_permission(guild_id: str, user_id: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO permissions (guild_id, user_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (guild_id, user_id)
            )
        conn.commit()
    finally:
        put_conn(conn)

def db_remove_permission(guild_id: str, user_id: str) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM permissions WHERE guild_id=%s AND user_id=%s",
                (guild_id, user_id)
            )
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    finally:
        put_conn(conn)

def db_insert_log(message: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO logs (message, timestamp) VALUES (%s, NOW())",
                (message,)
            )
        conn.commit()
    finally:
        put_conn(conn)

def db_get_recent_logs(limit: int = 200) -> List[str]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT message, timestamp FROM logs ORDER BY id DESC LIMIT %s",
                (limit,)
            )
            rows = cur.fetchall()
        # Return in chronological order with formatted timestamps
        return [
            f"[{ts.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
            for msg, ts in reversed(rows)
        ]
    finally:
        put_conn(conn)

# ===========================
#       LOGGING (DB)
# ===========================

def log_event(msg: str):
    """Persist event to the logs table and keep last 200 in memory."""
    global logs_cache
    ts = discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    entry = f"[{ts}] {msg}"
    logs_cache.append(entry)
    if len(logs_cache) > 200:
        logs_cache = logs_cache[-200:]
    try:
        db_insert_log(msg)
    except Exception as e:
        print(f"⚠️ Log DB error: {e}")

# ===========================
#    PERMISSION SYSTEM
# ===========================

def is_owner(user) -> bool:
    return user.name.lower() == OWNER_USERNAME.lower()

def has_allowed_role(member) -> bool:
    if not hasattr(member, 'roles'): return False
    return any(role.name.lower() in ALLOWED_ROLES for role in member.roles)

def is_permitted_user(guild_id: str, user_id: str) -> bool:
    try:
        return db_is_permitted(guild_id, user_id)
    except Exception:
        return False

def can_use_critical(ctx) -> bool:
    return (is_owner(ctx.author) or
            has_allowed_role(ctx.author) or
            is_permitted_user(str(ctx.guild.id), str(ctx.author.id)))

def critical_command():
    async def predicate(ctx):
        if can_use_critical(ctx): return True
        await ctx.send("🔒 **Permission Denied** — Owner, Mod, or Permitted only.")
        return False
    return commands.check(predicate)

# ===========================
#    CONFIRMATION BUTTONS
# ===========================

class ConfirmView(discord.ui.View):
    def __init__(self, ctx, confirm_callback):
        super().__init__(timeout=30)
        self.ctx = ctx
        self.confirm_callback = confirm_callback
        self.value = None
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("You cannot use this button.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except:
                pass

# ===========================
#    STATIC DATA LOADER
# ===========================

def load_json(filename: str, default=None):
    if default is None: default = {}
    if not os.path.exists(filename): return default
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            return json.loads(content) if content else default
    except Exception:
        return default

def save_json(filename: str, data) -> bool:
    """Used only for data.json (static boss data)."""
    try:
        tmp = filename + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, separators=(',', ': '))
        os.replace(tmp, filename)
        return True
    except Exception as e:
        print(f"❌ Save error: {e}")
        return False

def load_spawn_data():
    """Load static boss data from data.json and rebuild lookup tables."""
    global SPAWN_DATA
    SPAWN_DATA = load_json(DATA_FILE, {})
    BOSS_LOOKUP.clear()
    BOSS_NAME_LOOKUP.clear()
    CATEGORY_LOOKUP.clear()

    if SPAWN_DATA:
        for cat_name, bosses in SPAWN_DATA.items():
            for boss in bosses:
                if "spawn" not in boss or "window" not in boss:
                    print(f"⚠️ Missing data for '{boss.get('name', '?')}'")
                name_lower = boss["name"].lower()
                BOSS_NAME_LOOKUP[name_lower] = boss
                CATEGORY_LOOKUP[name_lower] = cat_name
                BOSS_LOOKUP[name_lower] = boss
                for shortcut, full in BOSS_SHORTCUTS.items():
                    if full.lower() == name_lower:
                        BOSS_LOOKUP[shortcut.lower()] = boss

    print(f"✅ Loaded: {len(BOSS_NAME_LOOKUP)} bosses")

# ===========================
#     CORE UTILITIES
# ===========================

def find_boss(query: str) -> Optional[dict]:
    if not query: return None
    q = query.strip().lower()
    if q in BOSS_LOOKUP: return BOSS_LOOKUP[q]
    for name, boss in BOSS_NAME_LOOKUP.items():
        if q in name: return boss
    return None

def parse_duration(s: str) -> timedelta:
    if not s: return timedelta(0)
    s = str(s).lower()
    total = 0
    patterns = [
        (r'(\d+)\s*d(?:ays?)?', 86400),
        (r'(\d+)\s*h(?:ours?)?', 3600),
        (r'(\d+)\s*m(?:ins?|inutes?)?', 60),
        (r'(\d+)\s*s(?:ecs?|econds?)?', 1)
    ]
    for pattern, multiplier in patterns:
        match = re.search(pattern, s)
        if match: total += int(match.group(1)) * multiplier
    return timedelta(seconds=total)

def format_time(td: timedelta) -> str:
    secs = max(0, int(td.total_seconds()))
    if secs == 0: return "0s"
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s and not d: parts.append(f"{s}s")
    return " ".join(parts) if parts else "0s"

# ===========================
#   PROGRESS BAR SYSTEM
# ===========================

def make_progress_bar(percent: float, length: int = 12) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int((percent / 100) * length)
    empty = length - filled
    
    if percent < 25:   fill_char = "🔴"
    elif percent < 50: fill_char = "🟠"
    elif percent < 75: fill_char = "🟡"
    else:              fill_char = "🟢"
    
    return f"{fill_char * filled}{'⚫' * empty} `{percent:.0f}%`"

def get_spawn_progress(boss: dict, last_kill: datetime, now: datetime) -> Tuple[float, timedelta, datetime]:
    spawn_duration = parse_duration(boss["spawn"])
    next_spawn = last_kill + spawn_duration
    remaining = next_spawn - now
    
    if spawn_duration.total_seconds() <= 0:
        return 100.0, timedelta(0), next_spawn
    
    elapsed = (now - last_kill).total_seconds()
    total = spawn_duration.total_seconds()
    progress = max(0.0, min(100.0, (elapsed / total) * 100))
    
    return progress, remaining, next_spawn

def get_window_progress(boss: dict, next_spawn: datetime, now: datetime) -> Tuple[float, timedelta, datetime]:
    window_duration = parse_duration(boss["window"])
    window_end = next_spawn + window_duration
    time_left = window_end - now
    
    if window_duration.total_seconds() <= 0:
        return 100.0, timedelta(0), window_end
    
    elapsed = (now - next_spawn).total_seconds()
    total = window_duration.total_seconds()
    progress = max(0.0, min(100.0, (elapsed / total) * 100))
    
    return progress, time_left, window_end

def get_status(boss: dict, last_kill: datetime, now: datetime, is_raid: bool = False) -> str:
    progress, remaining, next_spawn = get_spawn_progress(boss, last_kill, now)
    
    if remaining.total_seconds() > 0:
        bar = make_progress_bar(progress)
        return f"⏳ {format_time(remaining)}\n{bar}"
    
    window_progress, window_left, window_end = get_window_progress(boss, next_spawn, now)
    
    if now <= window_end:
        bar = make_progress_bar(window_progress)
        if is_raid:
            chance = int(5 + (window_progress / 100) * 95)
            return f"🟢 OPEN ({chance}%)\n{bar}\n⏳ {format_time(window_left)} left"
        return f"🟢 OPEN\n{bar}\n⏳ {format_time(window_left)} left"
    
    return "⛔ Passed"

def split_embed_lines(lines: List[str], max_chars: int = 4000) -> List[str]:
    chunks, current, current_len = [], [], 0
    for line in lines:
        l_len = len(line) + 1
        if current_len + l_len > max_chars and current:
            chunks.append("\n".join(current))
            current, current_len = [line], l_len
        else:
            current.append(line)
            current_len += l_len
    if current: chunks.append("\n".join(current))
    return chunks

# ===========================
#     BACKGROUND TASKS
# ===========================

@tasks.loop(seconds=15)
async def check_spawns():
    if not SPAWN_DATA: return
    now = discord.utils.utcnow()
    try:
        all_channels = await asyncio.get_event_loop().run_in_executor(None, db_get_all_channels)
        all_timers   = await asyncio.get_event_loop().run_in_executor(None, db_get_all_timers)
    except Exception as e:
        print(f"⚠️ check_spawns DB error: {e}")
        return

    active_guilds = set(all_timers.keys()) & set(all_channels.keys())

    for guild_id in active_guilds:
        channel = bot.get_channel(int(all_channels[guild_id]))
        if not channel: continue

        for boss_key, last_kill in list(all_timers[guild_id].items()):
            boss = BOSS_NAME_LOOKUP.get(boss_key)
            if not boss: continue

            # Ensure last_kill is timezone-aware
            if last_kill.tzinfo is None:
                last_kill = last_kill.replace(tzinfo=discord.utils.utcnow().tzinfo)

            spawn_d   = parse_duration(boss["spawn"])
            window_d  = parse_duration(boss["window"])
            next_spawn = last_kill + spawn_d
            window_end = next_spawn + window_d
            double_end = next_spawn + (window_d * 2)

            base_key = f"{guild_id}_{boss_key}"
            key_soon = f"{base_key}_soon"
            key_open = f"{base_key}_open"

            try:
                if next_spawn - timedelta(minutes=5) <= now < next_spawn:
                    already = await asyncio.get_event_loop().run_in_executor(None, db_is_notified, key_soon)
                    if not already:
                        embed = discord.Embed(
                            title="⚠️ Spawning Soon!",
                            description=f"**{boss['name']}** in **< 5 minutes!**",
                            color=0xffa500, timestamp=now
                        )
                        await channel.send(embed=embed)
                        await asyncio.get_event_loop().run_in_executor(None, db_set_notified, key_soon)
                elif next_spawn <= now <= window_end:
                    already = await asyncio.get_event_loop().run_in_executor(None, db_is_notified, key_open)
                    if not already:
                        embed = discord.Embed(
                            title="🟢 BOSS IS OPEN!",
                            description=f"**{boss['name']}** has spawned!\nCloses <t:{int(window_end.timestamp())}:R>",
                            color=0x00ff00, timestamp=now
                        )
                        await channel.send(embed=embed)
                        await asyncio.get_event_loop().run_in_executor(None, db_set_notified, key_open)
                elif now > double_end:
                    await asyncio.get_event_loop().run_in_executor(None, db_delete_notified, key_soon)
                    await asyncio.get_event_loop().run_in_executor(None, db_delete_notified, key_open)
            except discord.Forbidden:
                pass
            except Exception as e:
                print(f"⚠️ Notify error: {e}")

@tasks.loop(hours=6)
async def cleanup_loop():
    try:
        cutoff = discord.utils.utcnow() - timedelta(days=30)
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Remove timers older than 30 days
                cur.execute("DELETE FROM timers WHERE last_kill < %s", (cutoff,))
                removed_timers = cur.rowcount
                # Remove stale notified keys that don't match any known boss
                cur.execute("SELECT notification_key FROM notified")
                all_keys = [row[0] for row in cur.fetchall()]
                stale = [k for k in all_keys if not any(b in k for b in BOSS_NAME_LOOKUP)]
                if stale:
                    cur.execute(
                        "DELETE FROM notified WHERE notification_key = ANY(%s)",
                        (stale,)
                    )
            conn.commit()
        finally:
            put_conn(conn)
        if removed_timers or stale:
            print(f"🧹 Cleaned: {len(stale)} notifications, {removed_timers} old timers")
    except Exception as e:
        print(f"⚠️ cleanup_loop error: {e}")

@check_spawns.before_loop
@cleanup_loop.before_loop
async def before_tasks(): await bot.wait_until_ready()

# ===========================
#    PERMISSION COMMANDS
# ===========================

@bot.command(name="permit", aliases=["addperm", "trust"])
async def permit_user(ctx, user: discord.Member = None):
    if not (is_owner(ctx.author) or has_allowed_role(ctx.author)): return await ctx.send("🔒 Owner/Mod only.")
    if not user: return await ctx.send("Usage: `!permit @user`")
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, db_add_permission, str(ctx.guild.id), str(user.id)
        )
    except Exception as e:
        return await ctx.send(f"❌ Database error: {e}")
    log_event(f"Permitted {user.name} by {ctx.author.name}")
    await ctx.send(f"✅ **{user.display_name}** can now use critical commands.")

@bot.command(name="unpermit", aliases=["removeperm", "revoke"])
async def unpermit_user(ctx, user: discord.Member = None):
    if not (is_owner(ctx.author) or has_allowed_role(ctx.author)): return await ctx.send("🔒 Owner/Mod only.")
    if not user: return await ctx.send("Usage: `!unpermit @user`")
    try:
        deleted = await asyncio.get_event_loop().run_in_executor(
            None, db_remove_permission, str(ctx.guild.id), str(user.id)
        )
    except Exception as e:
        return await ctx.send(f"❌ Database error: {e}")
    if deleted:
        log_event(f"Revoked {user.name} by {ctx.author.name}")
        return await ctx.send(f"🗑️ Removed permissions from **{user.display_name}**.")
    await ctx.send(f"⚠️ **{user.display_name}** wasn't permitted.")

@bot.command(name="perms")
async def list_perms(ctx):
    embed = discord.Embed(title="🔐 Permission List", color=0x9b59b6)
    embed.add_field(name="👑 Owner", value=f"`{OWNER_USERNAME}`", inline=False)
    embed.add_field(name="🛡️ Auto-Roles", value=", ".join(f"`{r}`" for r in ALLOWED_ROLES), inline=False)
    try:
        permitted = await asyncio.get_event_loop().run_in_executor(
            None, db_get_permitted_users, str(ctx.guild.id)
        )
    except Exception:
        permitted = []
    if permitted:
        users = [ctx.guild.get_member(int(uid)).mention if ctx.guild.get_member(int(uid)) else f"`{uid}`" for uid in permitted]
        embed.add_field(name=f"✅ Permitted ({len(permitted)})", value="\n".join(users), inline=False)
    else:
        embed.add_field(name="✅ Permitted", value="*None*", inline=False)
    await ctx.send(embed=embed)

# ===========================
#       MAIN COMMANDS
# ===========================

@bot.command(name="help", aliases=["h", "commands"])
async def help_cmd(ctx, *, page: str = None):
    """Optimized help command with clean pages"""
    if is_owner(ctx.author): perm_level = "👑 Owner"
    elif has_allowed_role(ctx.author): perm_level = "🛡️ Mod/Dev"
    elif is_permitted_user(str(ctx.guild.id), str(ctx.author.id)): perm_level = "✅ Permitted"
    else: perm_level = "👤 User"

    pages = {
        "main": {
            "title": "📚 Boss Timer Bot",
            "color": 0x2ecc71,
            "desc": "Track boss spawns with accurate timers, notifications, and statistics!",
            "fields": [
                ("⏱️ Timers", "`!help timers` — Kill, view, and set timers", False),
                ("📋 Lists", "`!help lists` — View categories and spawn data", False),
                ("⚙️ Config", "`!help config` — Set channels, pings, and more", False),
                ("🔐 Admin", "`!help admin` — Permissions and management", False),
                ("Your Access", f"**{perm_level}**", False)
            ],
            "footer": "Progress: 0% (just killed) → 100% (ready to spawn)"
        },
        "timers": {
            "title": "⏱️ Timer Commands",
            "color": 0x3498db,
            "desc": "Core commands for tracking bosses",
            "fields": [
                ("!kill <boss>", "Record a kill right now\n`!kill prot`\n⚠️ 5-minute anti-spam protection active", False),
                ("!timer <boss>", "View remaining time + progress bar", False),
                ("!set <boss> <time>", "Set timer (flexible)\n`!set prot 2d 5h 30m`", False),
                ("!next", "Show upcoming spawns", True),
                ("!up", "Show currently open bosses", True),
                ("🔒 !clear <boss>", "Delete one timer", True),
                ("🔒 !reset", "Delete ALL timers (with confirmation)", True)
            ],
            "footer": "🔒 = Requires permission | Duplicate kills within 5min are rejected"
        },
        "lists": {
            "title": "📋 Boss Lists",
            "color": 0x9b59b6,
            "desc": "View categories and spawn information",
            "fields": [
                ("Category Commands", "`!warden` `!meteoric` `!frozen`\n`!dl` `!edl` `!midr` `!raids`", False),
                ("!all", "Show all available categories", True),
                ("!list <category>", "Show detailed spawn data for a category", True),
                ("!shortcuts", "View all boss name shortcuts", True)
            ],
            "footer": "Use !list warden to see spawn and window times"
        },
        "config": {
            "title": "⚙️ Configuration",
            "color": 0xe67e22,
            "desc": "Server settings (Admin only)",
            "fields": [
                ("🔒 !setchannel", "Set current channel for notifications", False),
                ("🔒 !pingrole @role", "Set role to ping when boss opens", False),
                ("🔒 !editboss <boss> <time>", "Change base spawn duration", False),
                ("!backup", "Download boss data as zip", True)
            ],
            "footer": "Use !perms to manage who can run critical commands"
        },
        "admin": {
            "title": "🔐 Admin & Permissions",
            "color": 0xe74c3c,
            "desc": "Manage access and bot data",
            "fields": [
                ("Permission Levels", "👑 Owner (`evanora0`)\n🛡️ Roles: dev, moderator, mod, admin\n✅ Permitted users", False),
                ("🔒 !permit @user", "Grant critical command access", True),
                ("🔒 !unpermit @user", "Revoke access", True),
                ("!perms", "List all permitted users", True),
                ("🔒 !reload", "Reload boss data from data.json", True),
                ("🔒 !logs", "View recent bot actions (persisted in DB)", True)
            ],
            "footer": "Critical commands: !set, !clear, !reset, !editboss, !reload, !setchannel"
        }
    }

    page_key = "main"
    if page:
        page = page.lower()
        if page in ["timer", "timers", "time"]: page_key = "timers"
        elif page in ["list", "lists", "bosses"]: page_key = "lists"
        elif page in ["config", "setting", "settings", "setup"]: page_key = "config"
        elif page in ["admin", "perms", "permission", "permissions"]: page_key = "admin"

    data = pages[page_key]
    embed = discord.Embed(title=data["title"], description=data.get("desc", ""), color=data["color"])
    for name, value, inline in data.get("fields", []):
        embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text=data.get("footer", ""))
    await ctx.send(embed=embed)

@bot.command()
async def status(ctx):
    try:
        guild_timers = await asyncio.get_event_loop().run_in_executor(
            None, db_get_guild_timers, str(ctx.guild.id)
        )
        timer_count = len(guild_timers)
    except Exception:
        timer_count = 0
    embed = discord.Embed(title="📊 Bot Status", color=0x1abc9c)
    embed.add_field(name="Ping", value=f"{round(bot.latency*1000)}ms", inline=True)
    embed.add_field(name="Bosses", value=str(len(BOSS_NAME_LOOKUP)), inline=True)
    embed.add_field(name="Active Timers", value=str(timer_count), inline=True)
    embed.add_field(
        name="Your Access",
        value="👑 Owner" if is_owner(ctx.author) else (
            "🛡️ Mod" if has_allowed_role(ctx.author) else (
                "✅ Permitted" if is_permitted_user(str(ctx.guild.id), str(ctx.author.id)) else "👤 User"
            )
        ),
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command()
@critical_command()
async def reload(ctx):
    load_spawn_data()
    if check_spawns.is_running(): check_spawns.restart()
    log_event(f"Reloaded by {ctx.author.name}")
    await ctx.send("✅ Boss data reloaded!")

@bot.command()
async def kill(ctx, *, query: str):
    """Record a kill with 5-minute duplicate protection."""
    boss = find_boss(query)
    if not boss: return await ctx.send(f"❌ Boss not found: `{query}`")

    guild_id = str(ctx.guild.id)
    key = boss["name"].lower()
    now = discord.utils.utcnow()

    # === DUPLICATE KILL PROTECTION ===
    try:
        last_kill = await asyncio.get_event_loop().run_in_executor(
            None, db_get_timer, guild_id, key
        )
    except Exception as e:
        return await ctx.send(f"❌ Database error: {e}")

    if last_kill is not None:
        if last_kill.tzinfo is None:
            last_kill = last_kill.replace(tzinfo=now.tzinfo)
        time_since = now - last_kill
        if time_since < KILL_COOLDOWN:
            remaining = KILL_COOLDOWN - time_since
            return await ctx.send(
                f"⚠️ **{boss['name']}** was already killed **{format_time(time_since)}** ago.\n"
                f"Please wait `{format_time(remaining)}` before recording again."
            )

    try:
        await asyncio.get_event_loop().run_in_executor(
            None, db_upsert_timer, guild_id, key, now
        )
        await asyncio.get_event_loop().run_in_executor(
            None, db_delete_notified_prefix, f"{guild_id}_{key}_"
        )
    except Exception as e:
        return await ctx.send(f"❌ Database error: {e}")

    log_event(f"Kill: {boss['name']} by {ctx.author.name}")
    await ctx.send(f"✅ Kill recorded: **{boss['name']}** at <t:{int(now.timestamp())}:t>")

@bot.command(name="set", aliases=["settime", "sett"])
@critical_command()
@commands.cooldown(1, 5, commands.BucketType.guild)
async def set_timer(ctx, *, args: str):
    parts = args.split(" ", 1)
    if len(parts) < 2: return await ctx.send("❌ Format: `!set <boss> <time>`\nExample: `!set prot 2d 5h 30m`")
    boss_query, time_str = parts
    boss = find_boss(boss_query)
    if not boss: return await ctx.send(f"❌ Boss not found: `{boss_query}`")

    desired = parse_duration(time_str)
    if desired.total_seconds() == 0: return await ctx.send("❌ Invalid time. Use: `2d 5h 30m`, `12h`, `30m`")

    now = discord.utils.utcnow()
    spawn_d = parse_duration(boss["spawn"])
    fake_kill = (now + desired) - spawn_d

    guild_id = str(ctx.guild.id)
    key = boss["name"].lower()
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, db_upsert_timer, guild_id, key, fake_kill
        )
        await asyncio.get_event_loop().run_in_executor(
            None, db_delete_notified_prefix, f"{guild_id}_{key}_"
        )
    except Exception as e:
        return await ctx.send(f"❌ Database error: {e}")

    log_event(f"Set: {boss['name']} to {format_time(desired)} by {ctx.author.name}")
    await ctx.send(f"✅ **{boss['name']}** set to **{format_time(desired)}**")

@bot.command()
async def timer(ctx, *, query: str):
    boss = find_boss(query)
    if not boss: return await ctx.send("❌ Boss not found.")
    guild_id = str(ctx.guild.id)
    key = boss["name"].lower()
    try:
        last_kill = await asyncio.get_event_loop().run_in_executor(
            None, db_get_timer, guild_id, key
        )
    except Exception as e:
        return await ctx.send(f"❌ Database error: {e}")
    if last_kill is None:
        return await ctx.send(f"❌ No timer for **{boss['name']}**. Use `!kill {key}` first.")

    now = discord.utils.utcnow()
    if last_kill.tzinfo is None:
        last_kill = last_kill.replace(tzinfo=now.tzinfo)
    progress, remaining, next_spawn = get_spawn_progress(boss, last_kill, now)
    embed = discord.Embed(color=0x3498db)

    if remaining.total_seconds() > 0:
        bar = make_progress_bar(progress)
        embed.title = f"⏳ {boss['name']}"
        embed.description = f"**{format_time(remaining)}** until spawn\n{bar}\nSpawns <t:{int(next_spawn.timestamp())}:R>"
    else:
        window_prog, window_left, window_end = get_window_progress(boss, next_spawn, now)
        bar = make_progress_bar(window_prog)
        embed.title = f"🟢 {boss['name']} is OPEN!"
        embed.color = 0x00ff00
        embed.description = f"**{format_time(window_left)}** left in window\n{bar}\nCloses <t:{int(window_end.timestamp())}:R>"
    await ctx.send(embed=embed)

@bot.command()
async def next(ctx):
    guild_id = str(ctx.guild.id)
    try:
        guild_timers = await asyncio.get_event_loop().run_in_executor(
            None, db_get_guild_timers, guild_id
        )
    except Exception as e:
        return await ctx.send(f"❌ Database error: {e}")
    if not guild_timers: return await ctx.send("❌ No timers set.")
    now = discord.utils.utcnow()
    upcoming = []
    for boss_key, last_kill in guild_timers.items():
        boss = BOSS_NAME_LOOKUP.get(boss_key)
        if not boss: continue
        if last_kill.tzinfo is None:
            last_kill = last_kill.replace(tzinfo=now.tzinfo)
        remaining = (last_kill + parse_duration(boss["spawn"])) - now
        if remaining.total_seconds() > 0: upcoming.append((remaining, boss))
    if not upcoming: return await ctx.send("🟢 All bosses are UP or passed!")

    upcoming.sort(key=lambda x: x[0])
    embed = discord.Embed(title="⏭️ Next Bosses", color=0xe67e22)
    lines = []
    for i, (rem, boss) in enumerate(upcoming[:5]):
        spawn_dur = parse_duration(boss["spawn"])
        progress = max(0.0, min(100.0, ((spawn_dur - rem).total_seconds() / spawn_dur.total_seconds()) * 100)) if spawn_dur.total_seconds() > 0 else 100.0
        bar = make_progress_bar(progress, length=8)
        medal = ["🥇", "🥈", "🥉"][i] if i < 3 else "▫️"
        lines.append(f"{medal} **{boss['name']}** — {format_time(rem)} {bar}")
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)

@bot.command()
async def up(ctx):
    guild_id = str(ctx.guild.id)
    now = discord.utils.utcnow()
    try:
        guild_timers = await asyncio.get_event_loop().run_in_executor(
            None, db_get_guild_timers, guild_id
        )
    except Exception as e:
        return await ctx.send(f"❌ Database error: {e}")
    open_bosses = []
    for boss_key, last_kill in guild_timers.items():
        boss = BOSS_NAME_LOOKUP.get(boss_key)
        if not boss: continue
        if last_kill.tzinfo is None:
            last_kill = last_kill.replace(tzinfo=now.tzinfo)
        next_spawn = last_kill + parse_duration(boss["spawn"])
        window_end = next_spawn + parse_duration(boss["window"])
        if next_spawn <= now <= window_end: open_bosses.append(boss["name"])
    if not open_bosses:
        await ctx.send("❌ No bosses currently open.")
    else:
        embed = discord.Embed(title="🟢 Currently Open", description="\n".join(f"• {b}" for b in open_bosses), color=0x00ff00)
        await ctx.send(embed=embed)

@bot.command()
@critical_command()
async def clear(ctx, *, query: str):
    boss = find_boss(query)
    if not boss: return await ctx.send(f"❌ Boss not found: `{query}`")

    async def do_clear():
        guild_id = str(ctx.guild.id)
        key = boss["name"].lower()
        existing = await asyncio.get_event_loop().run_in_executor(
            None, db_get_timer, guild_id, key
        )
        if existing is not None:
            await asyncio.get_event_loop().run_in_executor(
                None, db_delete_timer, guild_id, key
            )
            await asyncio.get_event_loop().run_in_executor(
                None, db_delete_notified_prefix, f"{guild_id}_{key}_"
            )
            log_event(f"Clear: {boss['name']} by {ctx.author.name}")
            return f"🗑️ Deleted **{boss['name']}**"
        return f"❌ No timer for **{boss['name']}**"

    view = ConfirmView(ctx, do_clear)
    view.message = await ctx.send(f"Delete timer for **{boss['name']}**?", view=view)
    await view.wait()
    if view.value is True:
        result = await do_clear()
        await view.message.edit(content=result, view=None)
    elif view.value is False:
        await view.message.edit(content="❌ Cancelled.", view=None)
    else:
        await view.message.edit(content="⏱️ Timed out.", view=None)

@bot.command()
@critical_command()
async def reset(ctx):
    async def do_reset():
        guild_id = str(ctx.guild.id)
        guild_timers = await asyncio.get_event_loop().run_in_executor(
            None, db_get_guild_timers, guild_id
        )
        count = len(guild_timers)
        await asyncio.get_event_loop().run_in_executor(
            None, db_delete_guild_timers, guild_id
        )
        await asyncio.get_event_loop().run_in_executor(
            None, db_delete_notified_prefix, f"{guild_id}_"
        )
        if count > 0:
            log_event(f"Reset all by {ctx.author.name} ({count})")
        return f"🗑️ Reset **{count}** timers"

    view = ConfirmView(ctx, do_reset)
    view.message = await ctx.send("⚠️ Delete **ALL** timers?", view=view)
    await view.wait()
    if view.value is True:
        result = await do_reset()
        await view.message.edit(content=result, view=None)
    elif view.value is False:
        await view.message.edit(content="❌ Cancelled.", view=None)
    else:
        await view.message.edit(content="⏱️ Timed out.", view=None)

@bot.command()
@critical_command()
async def editboss(ctx, *, args: str):
    global SPAWN_DATA
    parts = args.split(" ", 1)
    if len(parts) < 2: return await ctx.send("❌ Format: `!editboss <boss> <time>`")
    boss_query, time_str = parts
    new_dur = parse_duration(time_str)
    if new_dur.total_seconds() == 0: return await ctx.send("❌ Invalid duration")
    boss_found = find_boss(boss_query)
    if not boss_found: return await ctx.send(f"❌ Boss not found: `{boss_query}`")
    dur_str = f"{new_dur.days}d {new_dur.seconds//3600}h {(new_dur.seconds%3600)//60}m"
    for cat, bosses in SPAWN_DATA.items():
        for b in bosses:
            if b["name"].lower() == boss_found["name"].lower():
                b["spawn"] = dur_str
                save_json(DATA_FILE, SPAWN_DATA)
                BOSS_LOOKUP[b["name"].lower()] = b
                for sc, full in BOSS_SHORTCUTS.items():
                    if full.lower() == b["name"].lower(): BOSS_LOOKUP[sc.lower()] = b
                log_event(f"Edit: {b['name']} spawn to {dur_str} by {ctx.author.name}")
                return await ctx.send(f"✅ **{b['name']}** spawn → `{dur_str}`")
    await ctx.send("❌ Not found in data")

@bot.command(name="list")
async def list_cat(ctx, *, cat: str = None):
    if not cat:
        embed = discord.Embed(title="Categories", color=0x9b59b6)
        for cmd, info in CATEGORY_COMMANDS.items():
            embed.add_field(name=f"!{cmd}", value=info["category"], inline=True)
        return await ctx.send(embed=embed)
    cat_name = None
    for cmd, info in CATEGORY_COMMANDS.items():
        if cat.lower() in [cmd, info["category"].lower()] or cat.lower() in info["aliases"]:
            cat_name = info["category"]
            break
    if not cat_name or cat_name not in SPAWN_DATA:
        return await ctx.send(f"❌ Category not found")
    lines = [f"**{b['name']}** (Lv.{b.get('level', '?')})" for b in SPAWN_DATA[cat_name]]
    for chunk in split_embed_lines(lines):
        await ctx.send(embed=discord.Embed(title=f"📋 {cat_name}", description=chunk, color=0x3498db))

@bot.command(name="shortcuts")
async def shortcuts(ctx):
    grouped = defaultdict(list)
    for sc, full in BOSS_SHORTCUTS.items():
        grouped[full].append(f"`{sc}`")
    lines = [f"**{name}**: {', '.join(shorts)}" for name, shorts in grouped.items()]
    for chunk in split_embed_lines(lines):
        await ctx.send(embed=discord.Embed(title="⚡ Shortcuts", description=chunk, color=0xe67e22))

for cmd_name, info in CATEGORY_COMMANDS.items():
    @bot.command(name=cmd_name, aliases=info["aliases"])
    async def cmd(ctx, cat_name=info["category"]):
        if not SPAWN_DATA or cat_name not in SPAWN_DATA: return await ctx.send("❌ Category not found")
        guild_id = str(ctx.guild.id)
        now = discord.utils.utcnow()
        is_raid = cat_name in RAID_CATEGORIES
        try:
            guild_timers = await asyncio.get_event_loop().run_in_executor(
                None, db_get_guild_timers, guild_id
            )
        except Exception:
            guild_timers = {}
        lines = []
        for boss in SPAWN_DATA[cat_name]:
            key = boss["name"].lower()
            last_kill = guild_timers.get(key)
            if last_kill is not None:
                if last_kill.tzinfo is None:
                    last_kill = last_kill.replace(tzinfo=now.tzinfo)
                try:
                    status = get_status(boss, last_kill, now, is_raid)
                except Exception:
                    status = "❌ Error"
            else:
                status = "❌ No timer"
            lines.append(f"**{boss['name']}**\n{status}")
        for chunk in split_embed_lines(lines):
            await ctx.send(embed=discord.Embed(title=f"📋 {cat_name}", description=chunk, color=0x3498db))

@bot.command(name='all')
async def all_cmd(ctx):
    embed = discord.Embed(title="📚 Categories", color=0x9b59b6)
    for cmd, info in CATEGORY_COMMANDS.items():
        embed.add_field(name=f"!{cmd}", value=info["category"], inline=True)
    await ctx.send(embed=embed)

@bot.command()
@critical_command()
async def setchannel(ctx):
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, db_upsert_channel, str(ctx.guild.id), str(ctx.channel.id)
        )
    except Exception as e:
        return await ctx.send(f"❌ Database error: {e}")
    log_event(f"Channel set to {ctx.channel.id} by {ctx.author.name}")
    await ctx.send(f"✅ Notifications set to {ctx.channel.mention}")

@bot.command()
@critical_command()
async def logs(ctx):
    global logs_cache
    if not logs_cache:
        # Try to populate from DB on first call
        try:
            logs_cache = await asyncio.get_event_loop().run_in_executor(None, db_get_recent_logs, 200)
        except Exception:
            pass
    if not logs_cache: return await ctx.send("📜 No logs recorded yet.")
    embed = discord.Embed(title="📜 Bot Activity Log", description="\n".join(logs_cache[-20:]), color=0x7289da)
    embed.set_footer(text=f"Total entries: {len(logs_cache)} | Stored in PostgreSQL")
    await ctx.send(embed=embed)

@bot.command()
@critical_command()
async def backup(ctx):
    """Download static boss data as a zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(DATA_FILE): zf.write(DATA_FILE)
    buf.seek(0)
    await ctx.send("📦 Backup (boss data only — all timer/channel/permission data is in PostgreSQL):",
                   file=discord.File(buf, "backup.zip"))

# ===========================
#       EVENTS
# ===========================

@bot.event
async def on_ready():
    print(f'✅ Online: {bot.user}')
    load_spawn_data()
    # Warm the in-memory log cache
    global logs_cache
    try:
        logs_cache = await asyncio.get_event_loop().run_in_executor(None, db_get_recent_logs, 200)
    except Exception as e:
        print(f"⚠️ Could not load logs from DB: {e}")
    if not check_spawns.is_running(): check_spawns.start()
    if not cleanup_loop.is_running(): cleanup_loop.start()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)): return
    if isinstance(error, commands.CommandOnCooldown): return await ctx.send(f"⏳ Wait {error.retry_after:.1f}s")
    print(f"Error: {error}")

async def shutdown():
    if db_pool:
        db_pool.closeall()
    await bot.close()

if sys.platform != "win32":
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_event_loop().add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
    except: pass


token = os.getenv("TOKEN")
if not token:
    raise RuntimeError("TOKEN env variable not set")

# Initialise DB before starting the bot (synchronous, safe at module level)
init_db()

bot.run(token)