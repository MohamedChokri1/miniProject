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
TIMERS_FILE = 'timers.json'
CHANNEL_FILE = 'channels.json'
NOTIFIED_FILE = 'notified.json'
PERMS_FILE = 'permissions.json'
LOGS_FILE = 'logs.json'

# --- Permission Configuration ---
OWNER_USERNAME = "evanora0"
ALLOWED_ROLES = {"dev", "moderator", "mod", "admin"}

# --- Anti-Spam Config ---
KILL_COOLDOWN = timedelta(minutes=5)  # Reject duplicate kills within 5 minutes

# --- In-Memory Cache ---
SPAWN_DATA: Optional[Dict] = None
BOSS_LOOKUP: Dict[str, dict] = {}
BOSS_NAME_LOOKUP: Dict[str, dict] = {}
CATEGORY_LOOKUP: Dict[str, str] = {}

timers_cache = defaultdict(dict)
channels_cache: Dict[str, str] = {}
notified_cache: Dict[str, bool] = {}
permissions_cache: Dict[str, List[str]] = {}
logs_cache: List[str] = []

file_lock = asyncio.Lock()
dirty = False

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
#       LOGGING (JSON)
# ===========================

def log_event(msg: str):
    """Append event to persistent JSON log."""
    global logs_cache
    ts = discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    entry = f"[{ts}] {msg}"
    logs_cache.append(entry)
    
    # Keep last 200 entries to prevent bloat
    if len(logs_cache) > 200:
        logs_cache = logs_cache[-200:]
    
    # Mark dirty so autosave persists it
    mark_dirty()

# ===========================
#    PERMISSION SYSTEM
# ===========================

def is_owner(user) -> bool:
    return user.name.lower() == OWNER_USERNAME.lower()

def has_allowed_role(member) -> bool:
    if not hasattr(member, 'roles'): return False
    return any(role.name.lower() in ALLOWED_ROLES for role in member.roles)

def is_permitted_user(guild_id: str, user_id: str) -> bool:
    return user_id in permissions_cache.get(guild_id, [])

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
#       FILE I/O
# ===========================

def load_json(filename: str, default=None):
    if default is None: default = []
    if not os.path.exists(filename): return default
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            return json.loads(content) if content else default
    except Exception:
        return default

def save_json(filename: str, data) -> bool:
    try:
        tmp = filename + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, separators=(',', ': '))
        os.replace(tmp, filename)
        return True
    except Exception as e:
        print(f"❌ Save error: {e}")
        return False

def mark_dirty():
    global dirty
    dirty = True

def sync_to_disk():
    global dirty
    if not dirty: return
    save_json(TIMERS_FILE, dict(timers_cache))
    save_json(CHANNEL_FILE, channels_cache)
    save_json(NOTIFIED_FILE, notified_cache)
    save_json(PERMS_FILE, permissions_cache)
    save_json(LOGS_FILE, logs_cache)
    dirty = False

def load_all_data():
    global SPAWN_DATA, timers_cache, channels_cache, notified_cache, permissions_cache, logs_cache
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

    timers_cache = defaultdict(dict, load_json(TIMERS_FILE, {}))
    channels_cache = load_json(CHANNEL_FILE, {})
    notified_cache = load_json(NOTIFIED_FILE, {})
    permissions_cache = load_json(PERMS_FILE, {})
    logs_cache = load_json(LOGS_FILE, [])
    
    print(f"✅ Loaded: {len(BOSS_NAME_LOOKUP)} bosses | {len(channels_cache)} channels | {len(timers_cache)} guilds | {len(logs_cache)} log entries")

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
    active_guilds = set(timers_cache.keys()) & set(channels_cache.keys())
    
    async with file_lock:
        for guild_id in active_guilds:
            channel = bot.get_channel(int(channels_cache[guild_id]))
            if not channel: continue
            
            for boss_key, last_str in list(timers_cache[guild_id].items()):
                boss = BOSS_NAME_LOOKUP.get(boss_key)
                if not boss: continue
                
                try: last_kill = datetime.fromisoformat(last_str)
                except ValueError: continue
                
                spawn_d = parse_duration(boss["spawn"])
                window_d = parse_duration(boss["window"])
                next_spawn = last_kill + spawn_d
                window_end = next_spawn + window_d
                double_end = next_spawn + (window_d * 2)
                
                base_key = f"{guild_id}_{boss_key}"
                key_soon = f"{base_key}_soon"
                key_open = f"{base_key}_open"
                
                try:
                    if next_spawn - timedelta(minutes=5) <= now < next_spawn:
                        if key_soon not in notified_cache:
                            embed = discord.Embed(title="⚠️ Spawning Soon!", description=f"**{boss['name']}** in **< 5 minutes!**", color=0xffa500, timestamp=now)
                            await channel.send(embed=embed)
                            notified_cache[key_soon] = True
                            mark_dirty()
                    elif next_spawn <= now <= window_end:
                        if key_open not in notified_cache:
                            embed = discord.Embed(title="🟢 BOSS IS OPEN!", description=f"**{boss['name']}** has spawned!\nCloses <t:{int(window_end.timestamp())}:R>", color=0x00ff00, timestamp=now)
                            await channel.send(embed=embed)
                            notified_cache[key_open] = True
                            mark_dirty()
                    elif now > double_end:
                        if notified_cache.pop(key_soon, None) or notified_cache.pop(key_open, None):
                            mark_dirty()
                except discord.Forbidden: pass
                except Exception as e: print(f"⚠️ Notify error: {e}")

@tasks.loop(minutes=2)
async def autosave():
    async with file_lock: sync_to_disk()

@tasks.loop(hours=6)
async def cleanup_loop():
    async with file_lock:
        stale = [k for k in notified_cache if not any(b in k for b in BOSS_NAME_LOOKUP)]
        for k in stale: del notified_cache[k]
        
        cutoff = discord.utils.utcnow() - timedelta(days=30)
        removed = 0
        for guild_id in list(timers_cache.keys()):
            for boss_key, ts in list(timers_cache[guild_id].items()):
                try:
                    if datetime.fromisoformat(ts) < cutoff:
                        del timers_cache[guild_id][boss_key]
                        removed += 1
                except: pass
        
        if stale or removed:
            mark_dirty()
            print(f"🧹 Cleaned: {len(stale)} notifications, {removed} old timers")

@check_spawns.before_loop
@autosave.before_loop
@cleanup_loop.before_loop
async def before_tasks(): await bot.wait_until_ready()

# ===========================
#    PERMISSION COMMANDS
# ===========================

@bot.command(name="permit", aliases=["addperm", "trust"])
async def permit_user(ctx, user: discord.Member = None):
    if not (is_owner(ctx.author) or has_allowed_role(ctx.author)): return await ctx.send("🔒 Owner/Mod only.")
    if not user: return await ctx.send("Usage: `!permit @user`")
    async with file_lock:
        permissions_cache.setdefault(str(ctx.guild.id), []).append(str(user.id))
        mark_dirty()
    log_event(f"Permitted {user.name} by {ctx.author.name}")
    await ctx.send(f"✅ **{user.display_name}** can now use critical commands.")

@bot.command(name="unpermit", aliases=["removeperm", "revoke"])
async def unpermit_user(ctx, user: discord.Member = None):
    if not (is_owner(ctx.author) or has_allowed_role(ctx.author)): return await ctx.send("🔒 Owner/Mod only.")
    if not user: return await ctx.send("Usage: `!unpermit @user`")
    async with file_lock:
        perms = permissions_cache.get(str(ctx.guild.id), [])
        if str(user.id) in perms:
            perms.remove(str(user.id))
            mark_dirty()
            log_event(f"Revoked {user.name} by {ctx.author.name}")
            return await ctx.send(f"🗑️ Removed permissions from **{user.display_name}**.")
    await ctx.send(f"⚠️ **{user.display_name}** wasn't permitted.")

@bot.command(name="perms")
async def list_perms(ctx):
    embed = discord.Embed(title="🔐 Permission List", color=0x9b59b6)
    embed.add_field(name="👑 Owner", value=f"`{OWNER_USERNAME}`", inline=False)
    embed.add_field(name="🛡️ Auto-Roles", value=", ".join(f"`{r}`" for r in ALLOWED_ROLES), inline=False)
    permitted = permissions_cache.get(str(ctx.guild.id), [])
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
                ("!backup", "Download all server data as zip", True)
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
                ("🔒 !reload", "Reload all data from files", True),
                ("🔒 !logs", "View recent bot actions (persisted in JSON)", True)
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
    embed = discord.Embed(title="📊 Bot Status", color=0x1abc9c)
    embed.add_field(name="Ping", value=f"{round(bot.latency*1000)}ms", inline=True)
    embed.add_field(name="Bosses", value=str(len(BOSS_NAME_LOOKUP)), inline=True)
    embed.add_field(name="Active Timers", value=str(sum(len(t) for t in timers_cache.values())), inline=True)
    embed.add_field(name="Your Access", value="👑 Owner" if is_owner(ctx.author) else ("🛡️ Mod" if has_allowed_role(ctx.author) else ("✅ Permitted" if is_permitted_user(str(ctx.guild.id), str(ctx.author.id)) else "👤 User")), inline=False)
    await ctx.send(embed=embed)

@bot.command()
@critical_command()
async def reload(ctx):
    load_all_data()
    if check_spawns.is_running(): check_spawns.restart()
    log_event(f"Reloaded by {ctx.author.name}")
    await ctx.send("✅ Bot reloaded!")

@bot.command()
async def kill(ctx, *, query: str):
    """Record a kill with 5-minute duplicate protection."""
    boss = find_boss(query)
    if not boss: return await ctx.send(f"❌ Boss not found: `{query}`")
    
    guild_id = str(ctx.guild.id)
    key = boss["name"].lower()
    now = discord.utils.utcnow()
    
    # === DUPLICATE KILL PROTECTION ===
    if guild_id in timers_cache and key in timers_cache[guild_id]:
        try:
            last_kill = datetime.fromisoformat(timers_cache[guild_id][key])
            time_since = now - last_kill
            
            if time_since < KILL_COOLDOWN:
                remaining = KILL_COOLDOWN - time_since
                return await ctx.send(
                    f"⚠️ **{boss['name']}** was already killed **{format_time(time_since)}** ago.\n"
                    f"Please wait `{format_time(remaining)}` before recording again."
                )
        except ValueError:
            pass  # Corrupt data, proceed with new kill
    
    async with file_lock:
        timers_cache[guild_id][key] = now.isoformat()
        prefix = f"{guild_id}_{key}_"
        for k in list(notified_cache.keys()):
            if k.startswith(prefix): del notified_cache[k]
        mark_dirty()
    
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
    
    async with file_lock:
        guild_id = str(ctx.guild.id)
        key = boss["name"].lower()
        timers_cache[guild_id][key] = fake_kill.isoformat()
        prefix = f"{guild_id}_{key}_"
        for k in list(notified_cache.keys()):
            if k.startswith(prefix): del notified_cache[k]
        mark_dirty()
    log_event(f"Set: {boss['name']} to {format_time(desired)} by {ctx.author.name}")
    await ctx.send(f"✅ **{boss['name']}** set to **{format_time(desired)}**")

@bot.command()
async def timer(ctx, *, query: str):
    boss = find_boss(query)
    if not boss: return await ctx.send("❌ Boss not found.")
    guild_id = str(ctx.guild.id)
    key = boss["name"].lower()
    if guild_id not in timers_cache or key not in timers_cache[guild_id]:
        return await ctx.send(f"❌ No timer for **{boss['name']}**. Use `!kill {key}` first.")
    try: last_kill = datetime.fromisoformat(timers_cache[guild_id][key])
    except ValueError: return await ctx.send("❌ Corrupt data. Use `!kill` to reset.")
    
    now = discord.utils.utcnow()
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
    if not timers_cache.get(guild_id): return await ctx.send("❌ No timers set.")
    now = discord.utils.utcnow()
    upcoming = []
    for boss_key, time_str in timers_cache[guild_id].items():
        boss = BOSS_NAME_LOOKUP.get(boss_key)
        if not boss: continue
        try:
            last = datetime.fromisoformat(time_str)
            remaining = (last + parse_duration(boss["spawn"])) - now
            if remaining.total_seconds() > 0: upcoming.append((remaining, boss))
        except ValueError: continue
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
    open_bosses = []
    for boss_key, time_str in timers_cache[guild_id].items():
        boss = BOSS_NAME_LOOKUP.get(boss_key)
        if not boss: continue
        try:
            last = datetime.fromisoformat(time_str)
            next_spawn = last + parse_duration(boss["spawn"])
            window_end = next_spawn + parse_duration(boss["window"])
            if next_spawn <= now <= window_end: open_bosses.append(boss["name"])
        except ValueError: continue
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
        async with file_lock:
            if guild_id in timers_cache and key in timers_cache[guild_id]:
                del timers_cache[guild_id][key]
                for k in list(notified_cache.keys()):
                    if k.startswith(f"{guild_id}_{key}_"): del notified_cache[k]
                mark_dirty()
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
        count = len(timers_cache.get(guild_id, {}))
        async with file_lock:
            if guild_id in timers_cache: del timers_cache[guild_id]
            for k in list(notified_cache.keys()):
                if k.startswith(f"{guild_id}_"): del notified_cache[k]
            if count > 0:
                mark_dirty()
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
        lines = []
        for boss in SPAWN_DATA[cat_name]:
            key = boss["name"].lower()
            if guild_id in timers_cache and key in timers_cache[guild_id]:
                try:
                    last = datetime.fromisoformat(timers_cache[guild_id][key])
                    status = get_status(boss, last, now, is_raid)
                except: status = "❌ Error"
            else: status = "❌ No timer"
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
    channels_cache[str(ctx.guild.id)] = str(ctx.channel.id)
    mark_dirty()
    await ctx.send(f"✅ Notifications set to {ctx.channel.mention}")

@bot.command()
@critical_command()
async def logs(ctx):
    if not logs_cache: return await ctx.send("📜 No logs recorded yet.")
    embed = discord.Embed(title="📜 Bot Activity Log", description="\n".join(logs_cache[-20:]), color=0x7289da)
    embed.set_footer(text=f"Total entries: {len(logs_cache)} | Stored in {LOGS_FILE}")
    await ctx.send(embed=embed)

@bot.command()
@critical_command()
async def backup(ctx):
    async with file_lock: sync_to_disk()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in [DATA_FILE, TIMERS_FILE, CHANNEL_FILE, NOTIFIED_FILE, PERMS_FILE, LOGS_FILE]:
            if os.path.exists(f): zf.write(f)
    buf.seek(0)
    await ctx.send("📦 Backup:", file=discord.File(buf, "backup.zip"))

# ===========================
#       EVENTS
# ===========================

@bot.event
async def on_ready():
    print(f'✅ Online: {bot.user}')
    load_all_data()
    if not check_spawns.is_running(): check_spawns.start()
    if not autosave.is_running(): autosave.start()
    if not cleanup_loop.is_running(): cleanup_loop.start()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)): return
    if isinstance(error, commands.CommandOnCooldown): return await ctx.send(f"⏳ Wait {error.retry_after:.1f}s")
    print(f"Error: {error}")

async def shutdown():
    async with file_lock: sync_to_disk()
    await bot.close()

if sys.platform != "win32":
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_event_loop().add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
    except: pass


token = os.getenv("TOKEN")
if not token:
    raise RuntimeError("TOKEN env variable not set")
bot.run(token)