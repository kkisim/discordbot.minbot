import os
import asyncio
import json
from datetime import datetime, timedelta
import time
import random
import logging
from typing import Dict, List, Any
from collections import deque

import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp

# 메시지 내용 읽기 허용
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
synced = False  # 앱 커맨드 동기화 여부

# 환경 설정
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "30"))  # 전체 대기열 제한
MAX_PER_USER = int(os.getenv("MAX_PER_USER", "10"))  # 사용자별 대기열 제한
ALLOWED_ROLE = os.getenv("ALLOWED_ROLE")  # 지정 시 해당 역할을 가진 유저만 제어
VOLUME_DB = float(os.getenv("BOT_VOLUME_DB", "-22"))  # 기본 출력 게인(dB), 음량을 낮추려면 더 음수로
STATE_FILE = os.getenv("BOT_STATE_FILE", "bot_state.json")
CMD_COOLDOWN = float(os.getenv("CMD_COOLDOWN", "2.0"))  # 초 단위, 0이면 해제
DELETE_COMMANDS = os.getenv("DELETE_COMMANDS", "true").lower() in ("1", "true", "yes", "on")
NEXON_API_KEY = os.getenv("NEXON_API_KEY")
FIFA_API_KEY = os.getenv("FIFA_API_KEY")

# yt-dlp 설정 (고음질 우선, 검색 허용)
ytdl_opts = {
    "format": "bestaudio[ext=webm][abr>=192]/bestaudio[abr>=160]/bestaudio/best",
    "quiet": True,
    "nocheckcertificate": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    # SABR 피하기 + JS 런타임 경고 완화용
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "default"]
        }
    },
}
ytdl = yt_dlp.YoutubeDL(ytdl_opts)

# 길드별 재생 대기열과 검색 캐시
queues: dict[int, deque] = {}
search_cache: dict[int, list[dict]] = {}
current_track: dict[int, dict | None] = {}
panels: dict[int, discord.Message] = {}
repeat_mode: dict[int, str] = {}  # off|one|all
shuffle_mode: dict[int, bool] = {}
track_messages: dict[int, discord.Message] = {}
last_command_at: dict[int, float] = {}
fc_spid_cache: list[dict] = []
fc_season_cache: dict[int, str] = {}
fc_position_cache: dict[int, str] = {}
fc_spid_map: dict[int, dict] = {}
fc_meta_loaded = False

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("musicbot")


def get_queue(guild_id: int) -> deque:
    if guild_id not in queues:
        queues[guild_id] = deque()
    return queues[guild_id]


def clear_search(guild_id: int):
    search_cache.pop(guild_id, None)


def enforce_voice_ctx(ctx, require_bot: bool):
    """Prefix 명령에서 호출: 봇이 연결돼 있으면 같은 채널인지 확인."""
    user_vc = ctx.author.voice
    bot_vc = ctx.voice_client
    if bot_vc:
        if user_vc is None or user_vc.channel != bot_vc.channel:
            return bot_vc, "현재 봇이 있는 음성 채널에 같이 있어야 합니다."
    else:
        if require_bot:
            return None, "봇이 음성 채널에 연결되어 있지 않습니다."
        if user_vc is None:
            return None, "음성 채널에 먼저 들어가 주세요."
    return bot_vc, None


def enforce_voice_interaction(interaction: discord.Interaction, require_bot: bool):
    """Slash 명령에서 호출: 봇이 연결돼 있으면 같은 채널인지 확인."""
    user_vc = interaction.user.voice
    bot_vc = interaction.guild.voice_client
    if bot_vc:
        if user_vc is None or user_vc.channel != bot_vc.channel:
            return bot_vc, "현재 봇이 있는 음성 채널에 같이 있어야 합니다."
    else:
        if require_bot:
            return None, "봇이 음성 채널에 연결되어 있지 않습니다."
        if user_vc is None:
            return None, "음성 채널에 먼저 들어가 주세요."
    return bot_vc, None


def check_role_ctx(ctx):
    if ALLOWED_ROLE and ALLOWED_ROLE not in [r.name for r in ctx.author.roles]:
        return f"이 명령은 `{ALLOWED_ROLE}` 역할만 사용할 수 있어요."
    return None


def check_role_interaction(interaction: discord.Interaction):
    if ALLOWED_ROLE and ALLOWED_ROLE not in [r.name for r in interaction.user.roles]:
        return f"이 명령은 `{ALLOWED_ROLE}` 역할만 사용할 수 있어요."
    return None


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "알 수 없음"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def check_queue_limits(guild_id: int, user_id: int) -> str | None:
    queue = get_queue(guild_id)
    if len(queue) >= MAX_QUEUE:
        return f"대기열이 가득 찼어요. (최대 {MAX_QUEUE}곡)"
    user_count = sum(1 for item in queue if item.get("requester_id") == user_id)
    if user_count >= MAX_PER_USER:
        return f"한 사람이 추가할 수 있는 최대 곡 수는 {MAX_PER_USER}곡이에요."
    return None


def check_cooldown(user_id: int) -> str | None:
    if CMD_COOLDOWN <= 0:
        return None
    now = time.time()
    last = last_command_at.get(user_id, 0)
    if now - last < CMD_COOLDOWN:
        remaining = CMD_COOLDOWN - (now - last)
        return f"잠시 후 다시 시도해 주세요. ({remaining:.1f}초)"
    last_command_at[user_id] = now
    return None


async def delete_track_message(guild_id: int):
    msg = track_messages.pop(guild_id, None)
    if msg:
        try:
            await msg.delete()
        except Exception:
            pass


async def maybe_delete_command(message: discord.Message):
    if not DELETE_COMMANDS:
        return
    try:
        await message.delete()
    except Exception:
        pass


async def nexon_get(endpoint: str, params: dict) -> dict:
    if not NEXON_API_KEY:
        raise ValueError("NEXON_API_KEY가 설정되지 않았습니다.")
    headers = {"x-nxopen-api-key": NEXON_API_KEY}
    url = f"https://open.api.nexon.com{endpoint}"
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, params=params, timeout=10) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise ValueError(f"API 오류 {resp.status}: {text}")
            return await resp.json()


async def get_ocid(character_name: str) -> str:
    data = await nexon_get("/maplestory/v1/id", {"character_name": character_name})
    ocid = data.get("ocid")
    if not ocid:
        raise ValueError("캐릭터를 찾지 못했습니다.")
    return ocid


def maple_today():
    # KST 기준 날짜(단순 +9h)
    return (datetime.utcnow() + timedelta(hours=9)).date().isoformat()


def auction_params(item_name: str):
    clean = item_name.strip().strip("<>").strip()
    return {"item_name": clean, "date": maple_today()}, clean


async def fc_get(endpoint: str, params: dict) -> dict:
    if not FIFA_API_KEY:
        raise ValueError("FIFA_API_KEY가 설정되지 않았습니다.")
    headers = {"x-nxopen-api-key": FIFA_API_KEY}
    url = f"https://open.api.nexon.com{endpoint}"
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, params=params, timeout=10) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise ValueError(f"API 오류 {resp.status}: {text}")
            return await resp.json()


async def fc_get_ouid(nickname: str) -> str:
    data = await fc_get("/fconline/v1/id", {"nickname": nickname})
    ouid = data.get("ouid")
    if not ouid:
        raise ValueError("계정을 찾지 못했습니다.")
    return ouid


async def ensure_fc_meta():
    global fc_meta_loaded, fc_spid_cache, fc_season_cache, fc_position_cache
    if fc_meta_loaded and fc_spid_cache and fc_season_cache and fc_position_cache:
        return
    # spid, season 메타
    spid = await fc_get("/static/fconline/meta/spid.json", {})
    season = await fc_get("/static/fconline/meta/seasonid.json", {})
    position = await fc_get("/static/fconline/meta/spposition.json", {})
    fc_spid_cache = spid if isinstance(spid, list) else []
    fc_spid_map = {p.get("id"): p for p in fc_spid_cache if p.get("id") is not None}
    fc_season_cache = {s.get("seasonId"): s.get("className") for s in (season or [])}
    fc_position_cache = {p.get("spposition"): p.get("desc") for p in (position or [])}
    fc_meta_loaded = True


def find_players_by_name(keyword: str, limit: int = 5) -> list[dict]:
    kw = keyword.lower()
    results = []
    for p in fc_spid_cache:
        name = p.get("name", "")
        if kw in name.lower():
            results.append(p)
        if len(results) >= limit:
            break
    return results


def fc_player_image(spid: int) -> str:
    return f"https://fo4.dn.nexoncdn.co.kr/live/externalAssets/common/playersAction/p{spid}.png"


def fc_pretty_player(p: dict) -> str:
    pname = p.get("name") or "이름없음"
    spid = p.get("id")
    # spid 규칙: seasonId * 1,000,000 + pid
    season_id = p.get("season") or p.get("seasonId")
    if season_id is None and isinstance(spid, int):
        season_id = spid // 1_000_000
    season_name = fc_season_cache.get(season_id, str(season_id) if season_id is not None else "-")
    pos_code = p.get("spposition")
    if pos_code is None:
        pos_code = p.get("position")
    if pos_code is None:
        pos_name = "포지션 정보 없음"
    else:
        pos_name = fc_position_cache.get(pos_code, str(pos_code))
    return f"{pname} ({season_name}) | 포지션: {pos_name}"


def fc_pretty_player_by_id(spid: int) -> str:
    info = fc_spid_map.get(spid)
    if not info:
        return f"spid {spid}"
    return fc_pretty_player(info)


def save_state():
    data = {
        "queues": {},
        "repeat_mode": repeat_mode,
        "shuffle_mode": shuffle_mode,
    }
    for gid, q in queues.items():
        serial = []
        for item in q:
            serial.append(
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "web_url": item.get("web_url"),
                    "duration": item.get("duration"),
                    "thumbnail": item.get("thumbnail"),
                    "requester": item.get("requester"),
                    "requester_id": item.get("requester_id"),
                    "channel_id": item.get("channel_id"),
                }
            )
        data["queues"][gid] = serial
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Failed to save state: %s", exc)


def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("Failed to load state: %s", exc)
        return
    repeat_mode.update(data.get("repeat_mode", {}))
    shuffle_mode.update(data.get("shuffle_mode", {}))
    for gid_str, items in (data.get("queues") or {}).items():
        try:
            gid = int(gid_str)
        except Exception:
            continue
        dq = deque()
        for item in items:
            dq.append(item)
        queues[gid] = dq


# 초기 상태 로드
load_state()


@bot.event
async def on_ready():
    global synced
    if not synced:
        for g in bot.guilds:
            await tree.sync(guild=g)
        try:
            await tree.sync()
        except Exception:
            pass
        synced = True
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.command()
async def ping(ctx):
    await ctx.send("pong!")


@bot.command(name="helpme")
async def help_cmd(ctx):
    text = (
        "▶ 음악\n"
        f"- !p / !play <링크|검색어> (슬래시 /play도 가능). 대기열 {MAX_QUEUE}곡, 1인 {MAX_PER_USER}곡.\n"
        "- !search → 버튼 선택, !queue / !clear / !move / !remove / !skip / !stop / !pause / !resume / !panel\n"
        "- 같은 음성 채널에서만 제어. 안내 숨김은 QUIET_NOTICE, 명령 삭제는 DELETE_COMMANDS, 음량은 BOT_VOLUME_DB(기본 {VOLUME_DB}dB)\n"
        "\n▶ 메이플 (NEXON_API_KEY 필요, 슬래시도 동일 이름)\n"
        "- 기본: !ms / !msbasic(메이플기본), !msstat(능력치), !mspop(인기도)\n"
        "- 장비/스킬: !msequip(장비), !msskill(스킬), !mslink(링크스킬), !mspet(펫), !msandroid(안드로이드), !msbeauty(헤어성형)\n"
        "- 매트릭스: !msvmatrix(브이매트릭스), !mshexa(헥사), !mshexastat(헥사스탯)\n"
        "- 기타: !msdojo(무릉), !msotherstat(기타스탯), !msauc(경매) <아이템명>\n"
        "\n▶ FC온라인 (FIFA_API_KEY 필요, 슬래시도 동일 이름)\n"
        "- !fc / !fcbasic(피파기본) <닉네임>\n"
        "- !fcmax(피파등급), !fcmatch(피파경기) [matchtype 기본 50], !fctrade(피파거래)\n"
        "- !fcmatchdetail(피파전적): 최근 5경기 스코어/상대\n"
        "- !fcmeta(피파메타) [matchtype|season|division]\n"
        "- !fcplayer(선수검색) <이름>: 선수 목록(시즌/포지션/이미지)\n"
        "\n▶ 설정/실행\n"
        "- 필수: DISCORD_TOKEN, (선택) NEXON_API_KEY, FIFA_API_KEY\n"
        "- 자주 쓰는 옵션: BOT_VOLUME_DB, DELETE_COMMANDS, QUIET_NOTICE, MAX_QUEUE, MAX_PER_USER\n"
        "- 상태 저장: bot_state.json(STATE_FILE), 컨테이너/서버에서는 볼륨 마운트 권장\n"
        "\n기타: !미개, !매국"
    )
    await ctx.send(text)


@bot.command(name="미개")
async def mi_gae(ctx):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    await ctx.send("저는 미개한 김규민입니다")


@bot.command(name="매국")
async def mae_guk(ctx):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    await ctx.send("저는 매국 김규민 입니다")


@bot.command(name="msbasic", aliases=["ms", "메이플기본"])
async def ms_basic(ctx, *, character_name: str):
    """메이플 캐릭터 기본 정보 조회."""
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    if not NEXON_API_KEY:
        return await ctx.send("NEXON_API_KEY가 설정되지 않았습니다.")

    try:
        # 1) ocid 조회
        ocid_data = await nexon_get("/maplestory/v1/id", {"character_name": character_name})
        ocid = ocid_data.get("ocid")
        if not ocid:
            return await ctx.send("캐릭터를 찾지 못했습니다.")

        # 2) 기본 정보 조회
        basic = await nexon_get("/maplestory/v1/character/basic", {"ocid": ocid})
        name = basic.get("character_name", character_name)
        world = basic.get("world_name", "?")
        level = basic.get("character_level", "?")
        job = basic.get("character_class", "?")
        gender = basic.get("character_gender", "?")
        guild = basic.get("character_guild_name") or "-"
        create = basic.get("character_date_create") or "-"

        desc = (
            f"월드: {world}\n"
            f"레벨: {level}\n"
            f"직업: {job}\n"
            f"성별: {gender}\n"
            f"길드: {guild}\n"
            f"생성일: {create}"
        )
        embed = discord.Embed(title=f"{name} 기본 정보", description=desc, color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="msstat", aliases=["능력치"])
async def ms_stat(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        stat = await nexon_get("/maplestory/v1/character/stat", {"ocid": ocid})
        latest = (stat.get("stat") or [])[:8]
        lines = [f"{s.get('stat_name')}: {s.get('stat_value')}" for s in latest]
        embed = discord.Embed(title=f"{character_name} 종합 능력치", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="mspop", aliases=["인기도"])
async def ms_pop(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        pop = await nexon_get("/maplestory/v1/character/popularity", {"ocid": ocid})
        value = pop.get("popularity") or "?"
        embed = discord.Embed(title=f"{character_name} 인기도", description=f"인기도: {value}", color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="msequip", aliases=["장비"])
async def ms_equip(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        eq = await nexon_get("/maplestory/v1/character/item-equipment", {"ocid": ocid})
        items = (eq.get("item_equipment") or [])[:10]
        lines = []
        for it in items:
            name = it.get("item_name") or "이름없음"
            star = it.get("starforce") or 0
            main = it.get("item_option", [])
            first_opt = main[0]["option_value"] if main else ""
            lines.append(f"{name} ★{star} {first_opt}")
        embed = discord.Embed(title=f"{character_name} 장착 장비 (상위 10)", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="msskill", aliases=["스킬"])
async def ms_skill(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        skills = await nexon_get("/maplestory/v1/character/skill", {"ocid": ocid})
        list_skill = (skills.get("character_skill") or [])[:10]
        lines = [f"{s.get('skill_name')} Lv.{s.get('skill_level')}" for s in list_skill]
        embed = discord.Embed(title=f"{character_name} 스킬 (상위 10)", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="msauc", aliases=["경매"])
async def ms_auction(ctx, *, item_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        params, clean = auction_params(item_name)
        data = await nexon_get("/maplestory/v1/auction", params)
        rows = sorted(data.get("items") or [], key=lambda x: x.get("unit_price", 0))[:5]
        lines = [f"{r.get('item_name')} | {r.get('unit_price')}메소 x{r.get('count',1)}" for r in rows]
        embed = discord.Embed(title=f"경매장 시세: {clean}", description="\n".join(lines) or "데이터 없음", color=0xFEE75C)
        await ctx.send(embed=embed)
    except Exception as exc:
        msg = str(exc)
        if "OPENAPI00004" in msg or "valid parameter" in msg:
            await ctx.send("조회 실패: 아이템명을 정확히 입력해 주세요. 예) !경매 몽환의 벨트")
        else:
            await ctx.send(f"조회 실패: {exc}")


@bot.command(name="msbeauty", aliases=["헤어성형"])
async def ms_beauty(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/beauty-equipment", {"ocid": ocid})
        hair = data.get("character_hair") or "-"
        face = data.get("character_face") or "-"
        skin = data.get("character_skin_name") or "-"
        embed = discord.Embed(title=f"{character_name} 헤어/성형/피부", color=0x57F287)
        embed.add_field(name="헤어", value=hair, inline=False)
        embed.add_field(name="성형", value=face, inline=False)
        embed.add_field(name="피부", value=skin, inline=False)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="msandroid", aliases=["안드로이드"])
async def ms_android(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/android-equipment", {"ocid": ocid})
        android = data.get("android_name") or "-"
        hair = data.get("android_hair") or "-"
        face = data.get("android_face") or "-"
        embed = discord.Embed(title=f"{character_name} 안드로이드", color=0x57F287)
        embed.add_field(name="이름", value=android, inline=False)
        embed.add_field(name="헤어", value=hair, inline=True)
        embed.add_field(name="성형", value=face, inline=True)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="mspet", aliases=["펫"])
async def ms_pet(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/pet-equipment", {"ocid": ocid})
        pets = data.get("pet_equipment") or []
        lines = []
        for p in pets[:3]:
            lines.append(f"{p.get('pet_name')} | 장비: {p.get('pet_equipment_item_name') or '-'}")
        embed = discord.Embed(title=f"{character_name} 펫 정보", description="\n".join(lines) or "펫 없음", color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="mslink", aliases=["링크스킬"])
async def ms_link(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/link-skill", {"ocid": ocid})
        skills = (data.get("character_link_skill") or [])[:5]
        lines = [f"{s.get('skill_name')} Lv.{s.get('skill_level')}" for s in skills]
        embed = discord.Embed(title=f"{character_name} 링크 스킬", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="msvmatrix", aliases=["브이매트릭스"])
async def ms_vmatrix(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/vmatrix", {"ocid": ocid})
        cores = (data.get("character_v_core_equipment") or [])[:6]
        lines = [f"{c.get('v_core_name')} Lv.{c.get('v_core_level')}" for c in cores]
        embed = discord.Embed(title=f"{character_name} V매트릭스", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="mshexa", aliases=["헥사"])
async def ms_hexa(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/hexamatrix", {"ocid": ocid})
        skills = (data.get("character_hexacore_equipment") or [])[:6]
        lines = [f"{h.get('hexa_core_name')} Lv.{h.get('hexa_core_level')}" for h in skills]
        embed = discord.Embed(title=f"{character_name} HEXA 코어", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="mshexastat", aliases=["헥사스탯"])
async def ms_hexastat(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/hexamatrix-stat", {"ocid": ocid})
        stats = data.get("character_hexamatrix_stat_core") or []
        lines = []
        for s in stats[:5]:
            lines.append(f"{s.get('stat_core_name')} Lv.{s.get('stat_core_level')}")
        embed = discord.Embed(title=f"{character_name} HEXA 스탯", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="msdojo", aliases=["무릉"])
async def ms_dojo(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/dojang", {"ocid": ocid})
        floor = data.get("dojang_best_floor") or "?"
        rank = data.get("dojang_best_time_rank") or "?"
        time_val = data.get("dojang_best_time") or "?"
        embed = discord.Embed(title=f"{character_name} 무릉도장", color=0x57F287)
        embed.add_field(name="최고 층", value=floor, inline=True)
        embed.add_field(name="랭크", value=rank, inline=True)
        embed.add_field(name="기록", value=f"{time_val}초", inline=True)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="msotherstat", aliases=["기타스탯"])
async def ms_otherstat(ctx, *, character_name: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/other-stat", {"ocid": ocid})
        stats = data.get("character_additional_information") or []
        lines = [f"{s.get('stat_name')}: {s.get('stat_value')}" for s in stats[:8]]
        embed = discord.Embed(title=f"{character_name} 기타 능력치", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


# FC Online
@bot.command(name="fcbasic", aliases=["fc", "피파기본"])
async def fc_basic(ctx, *, nickname: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ouid = await fc_get_ouid(nickname)
        data = await fc_get("/fconline/v1/user/basic", {"ouid": ouid})
        level = data.get("level", "?")
        nickname = data.get("nickname", nickname)
        access = data.get("access_id", "-")
        desc = f"레벨: {level}\n닉네임: {nickname}\nAccess ID: {access}"
        embed = discord.Embed(title=f"{nickname} 기본 정보", description=desc, color=0x3498DB)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="fcmax", aliases=["피파등급"])
async def fc_max(ctx, *, nickname: str):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ouid = await fc_get_ouid(nickname)
        data = await fc_get("/fconline/v1/user/maxdivision", {"ouid": ouid})
        latest = data.get("maxdivision") or []
        lines = []
        for d in latest[:5]:
            lines.append(f"시즌:{d.get('seasonId')} | 등급:{d.get('division')} | 타입:{d.get('matchType')}")
        embed = discord.Embed(title=f"{nickname} 역대 최고 등급", description="\n".join(lines) or "데이터 없음", color=0x3498DB)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="fcmatch", aliases=["피파경기", "최근경기"])
async def fc_match(ctx, nickname: str, matchtype: str = "50"):
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ouid = await fc_get_ouid(nickname)
        params = {"ouid": ouid, "offset": 0, "limit": 5, "matchtype": matchtype}
        matches = await fc_get("/fconline/v1/user/match", params)
        ids = matches if isinstance(matches, list) else []
        lines = []
        for mid in ids[:5]:
            try:
                detail = await fc_get("/fconline/v1/match-detail", {"matchid": mid})
                infos = detail.get("matchInfo") or []
                if len(infos) < 2:
                    lines.append(f"{mid}: 상세 없음")
                    continue
                p1, p2 = infos[0], infos[1]
                mine = p1 if p1.get("ouid") == ouid else p2
                opp = p2 if mine is p1 else p1
                my_score = mine.get("shoot", {}).get("goalTotal") if mine else "?"
                opp_score = opp.get("shoot", {}).get("goalTotal") if opp else "?"
                opp_name = opp.get("nickname") if opp else "?"
                result = "무" if my_score == opp_score else ("승" if my_score > opp_score else "패")
                lines.append(f"{result} {my_score}:{opp_score} vs {opp_name}")
            except Exception as inner:
                lines.append(f"{mid}: 상세 실패 ({inner})")
        embed = discord.Embed(title=f"{nickname} 최근 경기 (최대 5)", description="\n".join(lines) or "데이터 없음", color=0x3498DB)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="fctrade", aliases=["피파거래"])
async def fc_trade(ctx, nickname: str, tradetype: str = "sell"):
    """tradetype: sell(판매) / buy(구매). 기본 sell."""
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        nickname = nickname.strip()
        tmap = {"sell": "sell", "buy": "buy", "판매": "sell", "구매": "buy"}
        tval = tmap.get(tradetype.lower())
        if not tval:
            return await ctx.send("tradetype은 sell(판매)/buy(구매) 중 하나를 입력하세요.")
        ouid = await fc_get_ouid(nickname)
        params = {"ouid": ouid, "tradetype": tval, "offset": 0, "limit": 5}
        try:
            data = await fc_get("/fconline/v1/user/trade", params)
        except ValueError as exc:
            if "OPENAPI00004" in str(exc):
                # tradetype 없이 재시도
                data = await fc_get("/fconline/v1/user/trade", {"ouid": ouid, "offset": 0, "limit": 5})
            else:
                raise
        rows = data.get("trades") if isinstance(data, dict) else data
        rows = rows or []
        lines = []
        for r in rows[:5]:
            item = r.get("spid") or "-"
            price = r.get("value") or "-"
            trade_type = r.get("tradeType") or tval
            lines.append(f"{trade_type} | 아이템:{item} | 가격:{price}")
        embed = discord.Embed(
            title=f"{nickname} 거래 기록(최근 5, {tval})",
            description="\n".join(lines) or "데이터 없음",
            color=0x3498DB,
        )
        await ctx.send(embed=embed)
    except Exception as exc:
        msg = str(exc)
        if "OPENAPI00004" in msg:
            await ctx.send("조회 실패: 닉네임을 확인하거나 거래 내역이 없는 경우일 수 있습니다. tradetype은 sell/buy만 지원하며, 없으면 자동 재시도합니다.")
        else:
            await ctx.send(f"조회 실패: {exc}")


@bot.command(name="fcmatchdetail", aliases=["피파전적"])
async def fc_matchdetail(ctx, nickname: str, matchtype: str = "50"):
    """최근 5경기 상대 닉네임과 스코어 요약"""
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        ouid = await fc_get_ouid(nickname)
        params = {"ouid": ouid, "offset": 0, "limit": 5, "matchtype": matchtype}
        match_ids = await fc_get("/fconline/v1/user/match", params)
        match_ids = match_ids if isinstance(match_ids, list) else []
        lines = []
        for mid in match_ids[:5]:
            try:
                detail = await fc_get("/fconline/v1/match-detail", {"matchid": mid})
                infos = detail.get("matchInfo") or []
                if len(infos) < 2:
                    lines.append(f"{mid}: 상세 없음")
                    continue
                p1, p2 = infos[0], infos[1]
                # 내 팀 판단
                mine = p1 if p1.get("ouid") == ouid else p2
                opp = p2 if mine is p1 else p1
                my_score = mine.get("shoot", {}).get("goalTotal") if mine else "?"
                opp_score = opp.get("shoot", {}).get("goalTotal") if opp else "?"
                opp_name = opp.get("nickname") if opp else "?"
                result = "무" if my_score == opp_score else ("승" if my_score > opp_score else "패")
                lines.append(f"{result} {my_score}:{opp_score} vs {opp_name}")
            except Exception as inner:
                lines.append(f"{mid}: 상세 실패 ({inner})")
        embed = discord.Embed(
            title=f"{nickname} 최근 경기 요약",
            description="\n".join(lines) or "데이터 없음",
            color=0x3498DB,
        )
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="fcplayer", aliases=["선수검색"])
async def fc_player(ctx, *, name: str):
    """선수 이름으로 검색 후 시즌/포지션/이미지 표시(최대 5개)"""
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    try:
        await ensure_fc_meta()
        matches = find_players_by_name(name, limit=5)
        if not matches:
            return await ctx.send("검색 결과가 없습니다.")
        lines = [fc_pretty_player(p) for p in matches]
        embed = discord.Embed(title=f"선수 검색: {name}", description="\n".join(lines), color=0x3498DB)
        first_spid = matches[0].get("id")
        if first_spid:
            embed.set_thumbnail(url=fc_player_image(first_spid))
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


@bot.command(name="fcmeta", aliases=["피파메타"])
async def fc_meta(ctx, meta_type: str = "matchtype"):
    """FC Online 메타데이터(매치타입/시즌/등급) 요약."""
    bot.loop.create_task(maybe_delete_command(ctx.message))
    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    meta_type = meta_type.lower()
    endpoint_map = {
        "matchtype": "/static/fconline/meta/matchtype.json",
        "season": "/static/fconline/meta/seasonid.json",
        "division": "/static/fconline/meta/division.json",
    }
    endpoint = endpoint_map.get(meta_type)
    if not endpoint:
        return await ctx.send("사용법: !fcmeta [matchtype|season|division]")
    try:
        data = await fc_get(endpoint, {})
        if isinstance(data, list):
            items = data[:10]
            if meta_type == "matchtype":
                lines = [f"{d.get('matchtype')}: {d.get('desc')}" for d in items]
            elif meta_type == "season":
                lines = [f"{d.get('seasonId')}: {d.get('className')}" for d in items]
            else:
                lines = [f"{d.get('divisionId')}: {d.get('divisionName')}" for d in items]
        else:
            lines = ["데이터 없음"]
        embed = discord.Embed(title=f"FC 메타 ({meta_type})", description="\n".join(lines) or "데이터 없음", color=0x3498DB)
        await ctx.send(embed=embed)
    except Exception as exc:
        await ctx.send(f"조회 실패: {exc}")


async def extract_stream(url: str):
    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
    except Exception as exc:
        raise ValueError(f"영상 정보를 불러오지 못했습니다: {exc}") from exc

    try:
        if "entries" in info:
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise ValueError("재생할 항목을 찾지 못했습니다.")
            info = entries[0]
        stream_url = info.get("url")
        if not stream_url:
            raise ValueError("스트림 URL이 없습니다.")
        return {
            "title": info.get("title", "제목 없음"),
            "url": stream_url,
            "web_url": info.get("webpage_url"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
        }
    except IndexError as exc:
        raise ValueError("재생할 항목을 찾지 못했습니다.") from exc
    except Exception as exc:
        raise ValueError(f"스트림 추출 중 오류: {exc}") from exc


async def search_tracks(query: str, limit: int = 7):
    loop = asyncio.get_event_loop()
    try:
        info = await loop.run_in_executor(None, lambda: ytdl.extract_info(f"ytsearch{limit}:{query}", download=False))
    except Exception as exc:
        raise ValueError(f"검색 실패: {exc}") from exc

    entries = [e for e in (info.get("entries") or []) if e]
    results = []
    for e in entries:
        title = e.get("title") or "제목 없음"
        url = e.get("webpage_url") or e.get("url")
        if url:
            results.append(
                {
                    "title": title,
                    "url": url,
                    "duration": e.get("duration"),
                    "thumbnail": e.get("thumbnail"),
                }
            )
    if not results:
        raise ValueError("검색 결과가 없습니다.")
    return results


def build_panel_embed(guild: discord.Guild) -> discord.Embed:
    voice = guild.voice_client
    track = current_track.get(guild.id)
    qlen = len(get_queue(guild.id))
    rep = repeat_mode.get(guild.id, "off")
    shuf = shuffle_mode.get(guild.id, False)

    if voice and voice.is_paused():
        status = "일시정지"
    elif voice and voice.is_playing():
        status = "재생 중"
    elif voice:
        status = "연결됨"
    else:
        status = "대기 중"

    desc = "재생 중인 곡이 없습니다."
    if track:
        title = track.get("title", "제목 없음")
        url = track.get("web_url") or track.get("url")
        requester = track.get("requester", "알 수 없음")
        duration = format_duration(track.get("duration"))
        if url:
            desc = f"[{title}]({url})\n요청자: {requester}\n길이: {duration}"
        else:
            desc = f"{title}\n요청자: {requester}\n길이: {duration}"
        thumb = track.get("thumbnail")

    embed = discord.Embed(title="음악 패널", description=desc, color=0x5865F2)
    embed.add_field(name="상태", value=status, inline=True)
    embed.add_field(name="대기열", value=f"{qlen} 곡", inline=True)
    if voice and voice.channel:
        embed.add_field(name="음성 채널", value=voice.channel.name, inline=True)
    embed.add_field(name="반복", value={"off": "끄기", "one": "한 곡 반복", "all": "대기열 반복"}.get(rep, "끄기"), inline=True)
    embed.add_field(name="셔플", value="On" if shuf else "Off", inline=True)
    if track and track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])
    return embed


class PlayerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _check_voice(self, interaction: discord.Interaction, require_bot=True):
        voice, err = enforce_voice_interaction(interaction, require_bot=require_bot)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return None
        return voice

    @discord.ui.button(label="⏯ 재생/일시정지", style=discord.ButtonStyle.primary)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice = await self._check_voice(interaction, require_bot=True)
        if not voice:
            return
        if voice.is_paused():
            voice.resume()
            msg = "다시 재생합니다."
        elif voice.is_playing():
            voice.pause()
            msg = "일시정지했습니다."
        else:
            msg = "재생 중이 아닙니다."
        await update_panel(interaction.guild)
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="⏭ 스킵", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice = await self._check_voice(interaction, require_bot=True)
        if not voice:
            return
        if not voice.is_playing():
            return await interaction.response.send_message("스킵할 재생이 없어요.", ephemeral=True)
        voice.stop()
        await update_panel(interaction.guild)
        await interaction.response.send_message("다음 곡으로 넘어갑니다.", ephemeral=True)

    @discord.ui.button(label="⏹ 정지", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice = await self._check_voice(interaction, require_bot=True)
        if not voice:
            return
        get_queue(interaction.guild.id).clear()
        clear_search(interaction.guild.id)
        current_track[interaction.guild.id] = None
        voice.stop()
        await update_panel(interaction.guild)
        await interaction.response.send_message("정지했습니다.", ephemeral=True)

    @discord.ui.button(label="🔄 새로고침", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await update_panel(interaction.guild)
        await interaction.response.send_message("패널을 새로고침했습니다.", ephemeral=True)

    @discord.ui.button(label="📜 대기열", style=discord.ButtonStyle.success)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice = await self._check_voice(interaction, require_bot=True)
        if not voice:
            return
        queue = get_queue(interaction.guild.id)
        if not queue:
            return await interaction.response.send_message("대기열이 비어 있어요.", ephemeral=True)
        lines = [f"{idx+1}. {item['title']}" for idx, item in enumerate(queue)]
        await interaction.response.send_message("대기열:\n" + "\n".join(lines), ephemeral=True)

    @discord.ui.button(label="🔁 반복", style=discord.ButtonStyle.secondary)
    async def toggle_repeat(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_err = check_role_interaction(interaction)
        if role_err:
            return await interaction.response.send_message(role_err, ephemeral=True)
        guild_id = interaction.guild.id
        current = repeat_mode.get(guild_id, "off")
        next_mode = {"off": "one", "one": "all", "all": "off"}[current]
        repeat_mode[guild_id] = next_mode
        await update_panel(interaction.guild)
        save_state()
        await interaction.response.send_message(f"반복 모드: {next_mode}", ephemeral=True)

    @discord.ui.button(label="🔀 셔플", style=discord.ButtonStyle.secondary)
    async def toggle_shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_err = check_role_interaction(interaction)
        if role_err:
            return await interaction.response.send_message(role_err, ephemeral=True)
        guild_id = interaction.guild.id
        shuffle_mode[guild_id] = not shuffle_mode.get(guild_id, False)
        await update_panel(interaction.guild)
        save_state()
        await interaction.response.send_message(f"셔플: {'On' if shuffle_mode[guild_id] else 'Off'}", ephemeral=True)


class SearchView(discord.ui.View):
    def __init__(self, guild_id: int, requester_id: int, is_ephemeral: bool):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.is_ephemeral = is_ephemeral
        results = search_cache.get(guild_id) or []
        for idx, item in enumerate(results[:5]):
            button = discord.ui.Button(label=f"{idx+1}", style=discord.ButtonStyle.primary, custom_id=f"pick_{idx}")
            button.callback = self._make_callback(idx)
            self.add_item(button)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.requester_id:
                return await interaction.response.send_message("검색을 시작한 사람만 선택할 수 있습니다.", ephemeral=True)
            cd_err = check_cooldown(interaction.user.id)
            if cd_err:
                return await interaction.response.send_message(cd_err, ephemeral=True)
            role_err = check_role_interaction(interaction)
            if role_err:
                return await interaction.response.send_message(role_err, ephemeral=True)
            voice, err = enforce_voice_interaction(interaction, require_bot=False)
            if err:
                return await interaction.response.send_message(err, ephemeral=True)
            if voice is None:
                voice = await interaction.user.voice.channel.connect()

            results = search_cache.get(self.guild_id) or []
            if index >= len(results):
                return await interaction.response.send_message("검색 결과가 만료되었습니다. 다시 검색해 주세요.", ephemeral=True)

            limit_err = check_queue_limits(self.guild_id, interaction.user.id)
            if limit_err:
                return await interaction.response.send_message(limit_err, ephemeral=True)

            base = results[index]
            track = {
                "title": base.get("title"),
                "url": base.get("url"),
                "web_url": base.get("web_url"),
                "duration": base.get("duration"),
                "thumbnail": base.get("thumbnail"),
                "channel": interaction.channel,
                "channel_id": interaction.channel.id,
                "requester": interaction.user.display_name,
                "requester_id": interaction.user.id,
            }
            queue = get_queue(self.guild_id)
            queue.append(track)

            if voice.is_playing() or voice.is_paused():
                await interaction.response.send_message(f"대기열에 추가: {track['title']}", ephemeral=True)
            else:
                await start_playback(interaction.guild, voice)
                await interaction.response.send_message(f"재생 시작: {track['title']}", ephemeral=True)
            await update_panel(interaction.guild, channel=interaction.channel)
            save_state()
        return callback


async def update_panel(guild: discord.Guild, channel: discord.abc.Messageable | None = None):
    """패널 메시지를 해당 길드에 대해 갱신."""
    embed = build_panel_embed(guild)
    view = PlayerView()
    msg = panels.get(guild.id)

    # 새 패널 채널이 지정되지 않았고, 기존 패널도 없으면 현재 트랙의 채널을 사용
    if channel is None and msg is None:
        track = current_track.get(guild.id)
        if track:
            channel = track.get("channel")

    # 기존 패널이 있으면 갱신 시도
    if msg:
        try:
            await msg.edit(embed=embed, view=view)
            return
        except Exception:
            panels.pop(guild.id, None)
    # 새로 생성
    if channel:
        try:
            new_msg = await channel.send(embed=embed, view=view)
            panels[guild.id] = new_msg
        except Exception:
            pass


async def start_playback(guild: discord.Guild, voice: discord.VoiceClient):
    queue = get_queue(guild.id)
    if not queue:
        return

    track = queue.popleft()
    # 셔플 모드일 때 무작위로 꺼내기
    if shuffle_mode.get(guild.id) and len(queue) > 1:
        idx = random.randrange(len(queue))
        track = queue[idx]
        del queue[idx]

    title = track["title"]
    stream_url = track["url"]
    channel = track.get("channel")
    channel_id = track.get("channel_id")
    if channel is None and channel_id:
        channel = bot.get_channel(channel_id)
    if channel is None:
        channel = voice.channel or guild.system_channel
    current_track[guild.id] = track

    ffmpeg_opts = {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        # volume 필터로 출력 음량 조절 (VOLUME_DB, 음수가 더 작음)
        "options": f"-vn -ac 2 -ar 48000 -b:a 192k -application audio -filter:a volume={VOLUME_DB}dB",
    }
    source = discord.FFmpegOpusAudio(stream_url, **ffmpeg_opts)

    def after_playback(error):
        bot.loop.call_soon_threadsafe(asyncio.create_task, handle_after(guild, error))

    voice.play(source, after=after_playback)
    # 이전 재생 알림 삭제 후 새 알림(가능하면 기존 메시지를 재활용)
    await delete_track_message(guild.id)
    if not QUIET_NOTICE and channel:
        try:
            msg = await channel.send(f"재생 시작: {title}")
            track_messages[guild.id] = msg
        except Exception:
            pass
    # repeat_all이면 재생된 곡을 큐 끝으로 보냄
    if repeat_mode.get(guild.id) == "all":
        get_queue(guild.id).append(track)
    await update_panel(guild, channel=channel)
    save_state()


async def handle_after(guild: discord.Guild, error: Exception | None):
    voice = guild.voice_client
    # 청취자가 없으면 자동 종료
    if voice and voice.channel:
        humans = [m for m in voice.channel.members if not m.bot]
        if not humans:
            get_queue(guild.id).clear()
            clear_search(guild.id)
            current_track[guild.id] = None
            await voice.disconnect()
            await update_panel(guild)
            save_state()
            return

    if error and voice:
        try:
            # 마지막에 재생한 채널 정보를 알 수 없으므로 길드 기본 시스템 채널이 있으면 거기로 보냄
            channel = guild.system_channel
            if channel:
                await channel.send(f"재생 중 오류: {error}")
        except Exception:
            pass
    if voice and not voice.is_playing() and not voice.is_paused():
        # repeat_one이면 현재 트랙 다시 재생
        if repeat_mode.get(guild.id) == "one" and current_track.get(guild.id):
            track = current_track[guild.id].copy()
            get_queue(guild.id).appendleft(track)
        await start_playback(guild, voice)

    # 다음 곡/반복 처리 후 상태 갱신
    if voice is None or (not voice.is_playing() and not get_queue(guild.id)):
        current_track[guild.id] = None
        await update_panel(guild)


@bot.command()
async def join(ctx):
    if ctx.author.voice is None:
        return await ctx.send("먼저 음성 채널에 들어가 주세요.")
    await ctx.author.voice.channel.connect()
    await ctx.send(f"{ctx.author.voice.channel.name} 채널에 연결했어요.")


@bot.command()
async def leave(ctx):
    role_err = check_role_ctx(ctx)
    if role_err:
        return await ctx.send(role_err)
    voice, err = enforce_voice_ctx(ctx, require_bot=True)
    if err:
        return await ctx.send(err)
    await voice.disconnect()
    get_queue(ctx.guild.id).clear()
    clear_search(ctx.guild.id)
    current_track[ctx.guild.id] = None
    await update_panel(ctx.guild)
    save_state()
    await ctx.send("음성 채널 연결을 끊었습니다.")


@bot.command(aliases=["p"])
async def play(ctx, *, url: str):
    try:
        cd_err = check_cooldown(ctx.author.id)
        if cd_err:
            return await ctx.send(cd_err)
        role_err = check_role_ctx(ctx)
        if role_err:
            return await ctx.send(role_err)
        voice, err = enforce_voice_ctx(ctx, require_bot=False)
        if err:
            return await ctx.send(err)
        if voice is None:
            voice = await ctx.author.voice.channel.connect()

        limit_err = check_queue_limits(ctx.guild.id, ctx.author.id)
        if limit_err:
            return await ctx.send(limit_err)

        info = await extract_stream(url)
        queue = get_queue(ctx.guild.id)
        info.update(
            {
                "channel": ctx.channel,
                "channel_id": ctx.channel.id,
                "requester": ctx.author.display_name,
                "requester_id": ctx.author.id,
            }
        )
        queue.append(info)

        if voice.is_playing() or voice.is_paused():
            await ctx.send(f"대기열에 추가: {info['title']}")
        else:
            await start_playback(ctx.guild, voice)
        await update_panel(ctx.guild, channel=ctx.channel)
        clear_search(ctx.guild.id)
        save_state()
    except Exception as exc:
        return await ctx.send(f"재생에 실패했습니다: {exc}")


@bot.command()
async def stop(ctx):
    role_err = check_role_ctx(ctx)
    if role_err:
        return await ctx.send(role_err)
    voice, err = enforce_voice_ctx(ctx, require_bot=True)
    if err:
        return await ctx.send(err)
    get_queue(ctx.guild.id).clear()
    clear_search(ctx.guild.id)
    current_track[ctx.guild.id] = None
    voice.stop()
    await update_panel(ctx.guild)
    save_state()
    await ctx.send("재생을 중지했어요.")


@bot.command()
async def pause(ctx):
    role_err = check_role_ctx(ctx)
    if role_err:
        return await ctx.send(role_err)
    voice, err = enforce_voice_ctx(ctx, require_bot=True)
    if err:
        return await ctx.send(err)
    if not voice.is_playing():
        return await ctx.send("재생 중이 아니에요.")
    if voice.is_paused():
        return await ctx.send("이미 일시정지 상태입니다.")
    voice.pause()
    await update_panel(ctx.guild)
    await ctx.send("일시정지했어요.")


@bot.command()
async def resume(ctx):
    role_err = check_role_ctx(ctx)
    if role_err:
        return await ctx.send(role_err)
    voice, err = enforce_voice_ctx(ctx, require_bot=True)
    if err:
        return await ctx.send(err)
    if not voice.is_paused():
        return await ctx.send("일시정지 상태가 아니에요.")
    voice.resume()
    await update_panel(ctx.guild)
    await ctx.send("다시 재생을 시작했어요.")


@bot.command()
async def skip(ctx):
    role_err = check_role_ctx(ctx)
    if role_err:
        return await ctx.send(role_err)
    voice, err = enforce_voice_ctx(ctx, require_bot=True)
    if err:
        return await ctx.send(err)
    if not voice.is_playing():
        return await ctx.send("스킵할 재생이 없어요.")
    voice.stop()
    await update_panel(ctx.guild)
    await ctx.send("다음 곡으로 넘어갔어요(대기열이 없으면 정지).")


@bot.command(name="queue")
async def queue_list(ctx):
    role_err = check_role_ctx(ctx)
    if role_err:
        return await ctx.send(role_err)
    voice, err = enforce_voice_ctx(ctx, require_bot=True)
    if err:
        return await ctx.send(err)
    queue = get_queue(ctx.guild.id)
    if not queue:
        return await ctx.send("대기열이 비어 있어요.")
    lines = [f"{idx+1}. {item['title']}" for idx, item in enumerate(queue)]
    await ctx.send("대기열:\n" + "\n".join(lines))


@bot.command(name="clear")
async def queue_clear(ctx):
    role_err = check_role_ctx(ctx)
    if role_err:
        return await ctx.send(role_err)
    voice, err = enforce_voice_ctx(ctx, require_bot=True)
    if err:
        return await ctx.send(err)
    queue = get_queue(ctx.guild.id)
    queue.clear()
    await update_panel(ctx.guild)
    save_state()
    await ctx.send("대기열을 비웠습니다.")


@bot.command(name="panel")
async def panel_cmd(ctx):
    role_err = check_role_ctx(ctx)
    if role_err:
        return await ctx.send(role_err)
    voice, err = enforce_voice_ctx(ctx, require_bot=False)
    if err:
        return await ctx.send(err)
    await update_panel(ctx.guild, channel=ctx.channel)
    await ctx.send("패널을 생성/업데이트했습니다.", delete_after=5)


@bot.command(name="move")
async def queue_move(ctx, src: int, dst: int):
    """대기열에서 src번째 트랙을 dst 위치로 이동 (1-based index)."""
    role_err = check_role_ctx(ctx)
    if role_err:
        return await ctx.send(role_err)
    voice, err = enforce_voice_ctx(ctx, require_bot=True)
    if err:
        return await ctx.send(err)
    queue = get_queue(ctx.guild.id)
    if not queue:
        return await ctx.send("대기열이 비어 있어요.")
    src -= 1
    dst -= 1
    if src < 0 or src >= len(queue) or dst < 0 or dst >= len(queue):
        return await ctx.send("인덱스가 잘못되었습니다.")
    item = queue[src]
    del queue[src]
    queue.insert(dst, item)
    await update_panel(ctx.guild)
    save_state()
    await ctx.send("순서를 변경했습니다.")


@bot.command(name="remove")
async def queue_remove(ctx, index: int):
    """대기열에서 index번째 트랙을 제거 (1-based index)."""
    role_err = check_role_ctx(ctx)
    if role_err:
        return await ctx.send(role_err)
    voice, err = enforce_voice_ctx(ctx, require_bot=True)
    if err:
        return await ctx.send(err)
    queue = get_queue(ctx.guild.id)
    if not queue:
        return await ctx.send("대기열이 비어 있어요.")
    index -= 1
    if index < 0 or index >= len(queue):
        return await ctx.send("인덱스가 잘못되었습니다.")
    removed = queue[index]["title"]
    del queue[index]
    await update_panel(ctx.guild)
    save_state()
    await ctx.send(f"대기열에서 제거했습니다: {removed}")


@bot.command(name="search")
async def search_cmd(ctx, *, query: str):
    role_err = check_role_ctx(ctx)
    if role_err:
        return await ctx.send(role_err)
    voice, err = enforce_voice_ctx(ctx, require_bot=False)
    if err:
        return await ctx.send(err)
    try:
        results = await search_tracks(query, limit=5)
    except Exception as exc:
        return await ctx.send(f"검색 실패: {exc}")
    search_cache[ctx.guild.id] = results
    embed = discord.Embed(title="검색 결과", description=f"`{query}`", color=0x57F287)
    for idx, item in enumerate(results[:5]):
        dur = format_duration(item.get("duration"))
        embed.add_field(name=f"{idx+1}. {item.get('title','제목 없음')}", value=f"길이: {dur}", inline=False)
    view = SearchView(ctx.guild.id, ctx.author.id, is_ephemeral=False)
    await ctx.send(embed=embed, view=view, suppress_embeds=True)


@bot.command(name="choose")
async def choose_cmd(ctx, index: int):
    results = search_cache.get(ctx.guild.id)
    if not results:
        return await ctx.send("먼저 !search 로 검색해 주세요.")
    index -= 1
    if index < 0 or index >= len(results):
        return await ctx.send("인덱스가 잘못되었습니다.")

    cd_err = check_cooldown(ctx.author.id)
    if cd_err:
        return await ctx.send(cd_err)
    role_err = check_role_ctx(ctx)
    if role_err:
        return await ctx.send(role_err)
    voice, err = enforce_voice_ctx(ctx, require_bot=False)
    if err:
        return await ctx.send(err)
    if voice is None:
        voice = await ctx.author.voice.channel.connect()

    limit_err = check_queue_limits(ctx.guild.id, ctx.author.id)
    if limit_err:
        return await ctx.send(limit_err)

    track = results[index]
    queue = get_queue(ctx.guild.id)
    track = {
        "title": track.get("title"),
        "url": track.get("url"),
        "web_url": track.get("web_url"),
        "duration": track.get("duration"),
        "thumbnail": track.get("thumbnail"),
        "channel": ctx.channel,
        "channel_id": ctx.channel.id,
        "requester": ctx.author.display_name,
        "requester_id": ctx.author.id,
    }
    queue.append(track)

    if voice.is_playing() or voice.is_paused():
        await ctx.send(f"대기열에 추가: {track['title']}")
    else:
        await start_playback(ctx.guild, voice)
    await update_panel(ctx.guild, channel=ctx.channel)
    save_state()


@tree.command(name="join", description="현재 음성 채널에 봇을 초대합니다.")
async def slash_join(interaction: discord.Interaction):
    if interaction.user.voice is None:
        return await interaction.response.send_message("먼저 음성 채널에 들어가 주세요.", ephemeral=True)
    await interaction.user.voice.channel.connect()
    await interaction.response.send_message(f"{interaction.user.voice.channel.name} 채널에 연결했어요.", ephemeral=True)


@tree.command(name="leave", description="봇을 음성 채널에서 내보냅니다.")
async def slash_leave(interaction: discord.Interaction):
    role_err = check_role_interaction(interaction)
    if role_err:
        return await interaction.response.send_message(role_err, ephemeral=True)
    if interaction.guild.voice_client is None:
        return await interaction.response.send_message("현재 연결된 음성 채널이 없어요.", ephemeral=True)
    await interaction.guild.voice_client.disconnect()
    get_queue(interaction.guild.id).clear()
    clear_search(interaction.guild.id)
    current_track[interaction.guild.id] = None
    await update_panel(interaction.guild)
    save_state()
    await interaction.response.send_message("음성 채널 연결을 끊었습니다.", ephemeral=True)


@tree.command(name="play", description="유튜브 링크의 오디오를 재생합니다.")
@app_commands.describe(url="유튜브 주소")
async def slash_play(interaction: discord.Interaction, url: str):
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True)

    try:
        cd_err = check_cooldown(interaction.user.id)
        if cd_err:
            return await interaction.followup.send(cd_err, ephemeral=True)
        role_err = check_role_interaction(interaction)
        if role_err:
            return await interaction.followup.send(role_err, ephemeral=True)
        voice, err = enforce_voice_interaction(interaction, require_bot=False)
        if err:
            return await interaction.followup.send(err, ephemeral=True)
        if voice is None:
            voice = await interaction.user.voice.channel.connect()

        limit_err = check_queue_limits(interaction.guild.id, interaction.user.id)
        if limit_err:
            return await interaction.followup.send(limit_err, ephemeral=True)

        info = await extract_stream(url)
        queue = get_queue(interaction.guild.id)
        info.update(
            {
                "channel": interaction.channel,
                "channel_id": interaction.channel.id,
                "requester": interaction.user.display_name,
                "requester_id": interaction.user.id,
            }
        )
        queue.append(info)

        if voice.is_playing() or voice.is_paused():
            await interaction.followup.send(f"대기열에 추가: {info['title']}", ephemeral=True)
        else:
            await start_playback(interaction.guild, voice)
        await update_panel(interaction.guild, channel=interaction.channel)
        clear_search(interaction.guild.id)
        save_state()
    except Exception as exc:
        try:
            await interaction.followup.send(f"재생에 실패했습니다: {exc}", ephemeral=True)
        except Exception:
            pass


@tree.command(name="stop", description="재생 중인 오디오를 중지합니다.")
async def slash_stop(interaction: discord.Interaction):
    role_err = check_role_interaction(interaction)
    if role_err:
        return await interaction.response.send_message(role_err, ephemeral=True)
    voice, err = enforce_voice_interaction(interaction, require_bot=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    get_queue(interaction.guild.id).clear()
    clear_search(interaction.guild.id)
    current_track[interaction.guild.id] = None
    voice.stop()
    await update_panel(interaction.guild)
    save_state()
    await interaction.response.send_message("재생을 중지했어요.", ephemeral=True)


@tree.command(name="pause", description="재생을 일시정지합니다.")
async def slash_pause(interaction: discord.Interaction):
    role_err = check_role_interaction(interaction)
    if role_err:
        return await interaction.response.send_message(role_err, ephemeral=True)
    voice, err = enforce_voice_interaction(interaction, require_bot=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if not voice.is_playing():
        return await interaction.response.send_message("재생 중이 아니에요.", ephemeral=True)
    if voice.is_paused():
        return await interaction.response.send_message("이미 일시정지 상태입니다.", ephemeral=True)
    voice.pause()
    await update_panel(interaction.guild)
    await interaction.response.send_message("일시정지했어요.", ephemeral=True)


@tree.command(name="resume", description="일시정지된 재생을 다시 시작합니다.")
async def slash_resume(interaction: discord.Interaction):
    role_err = check_role_interaction(interaction)
    if role_err:
        return await interaction.response.send_message(role_err, ephemeral=True)
    voice, err = enforce_voice_interaction(interaction, require_bot=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if not voice.is_paused():
        return await interaction.response.send_message("일시정지 상태가 아니에요.", ephemeral=True)
    voice.resume()
    await update_panel(interaction.guild)
    await interaction.response.send_message("다시 재생을 시작했어요.", ephemeral=True)


@tree.command(name="skip", description="현재 재생을 건너뜁니다.")
async def slash_skip(interaction: discord.Interaction):
    role_err = check_role_interaction(interaction)
    if role_err:
        return await interaction.response.send_message(role_err, ephemeral=True)
    voice, err = enforce_voice_interaction(interaction, require_bot=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if not voice.is_playing():
        return await interaction.response.send_message("스킵할 재생이 없어요.", ephemeral=True)
    voice.stop()
    await update_panel(interaction.guild)
    await interaction.response.send_message("다음 곡으로 넘어갔어요(대기열이 없으면 정지).", ephemeral=True)


@tree.command(name="queue", description="대기열을 보여줍니다.")
async def slash_queue(interaction: discord.Interaction):
    role_err = check_role_interaction(interaction)
    if role_err:
        return await interaction.response.send_message(role_err, ephemeral=True)
    voice, err = enforce_voice_interaction(interaction, require_bot=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    queue = get_queue(interaction.guild.id)
    if not queue:
        return await interaction.response.send_message("대기열이 비어 있어요.", ephemeral=True)
    lines = [f"{idx+1}. {item['title']}" for idx, item in enumerate(queue)]
    await interaction.response.send_message("대기열:\n" + "\n".join(lines), ephemeral=True)


@tree.command(name="clear", description="대기열을 비웁니다.")
async def slash_clear(interaction: discord.Interaction):
    role_err = check_role_interaction(interaction)
    if role_err:
        return await interaction.response.send_message(role_err, ephemeral=True)
    voice, err = enforce_voice_interaction(interaction, require_bot=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    queue = get_queue(interaction.guild.id)
    queue.clear()
    clear_search(interaction.guild.id)
    await update_panel(interaction.guild)
    save_state()
    await interaction.response.send_message("대기열을 비웠습니다.", ephemeral=True)


@tree.command(name="panel", description="음악 패널을 표시합니다.")
async def slash_panel(interaction: discord.Interaction):
    role_err = check_role_interaction(interaction)
    if role_err:
        return await interaction.response.send_message(role_err, ephemeral=True)
    voice, err = enforce_voice_interaction(interaction, require_bot=False)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    await update_panel(interaction.guild, channel=interaction.channel)
    await interaction.response.send_message("패널을 생성/업데이트했습니다.", ephemeral=True)


@tree.command(name="move", description="대기열 트랙 순서를 변경합니다.")
@app_commands.describe(src="이동할 트랙 번호(1부터)", dst="옮길 위치(1부터)")
async def slash_move(interaction: discord.Interaction, src: int, dst: int):
    role_err = check_role_interaction(interaction)
    if role_err:
        return await interaction.response.send_message(role_err, ephemeral=True)
    voice, err = enforce_voice_interaction(interaction, require_bot=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    queue = get_queue(interaction.guild.id)
    if not queue:
        return await interaction.response.send_message("대기열이 비어 있어요.", ephemeral=True)
    src -= 1
    dst -= 1
    if src < 0 or src >= len(queue) or dst < 0 or dst >= len(queue):
        return await interaction.response.send_message("인덱스가 잘못되었습니다.", ephemeral=True)
    item = queue[src]
    del queue[src]
    queue.insert(dst, item)
    await update_panel(interaction.guild)
    save_state()
    await interaction.response.send_message("순서를 변경했습니다.", ephemeral=True)


@tree.command(name="remove", description="대기열에서 특정 트랙을 제거합니다.")
@app_commands.describe(index="제거할 트랙 번호(1부터)")
async def slash_remove(interaction: discord.Interaction, index: int):
    role_err = check_role_interaction(interaction)
    if role_err:
        return await interaction.response.send_message(role_err, ephemeral=True)
    voice, err = enforce_voice_interaction(interaction, require_bot=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    queue = get_queue(interaction.guild.id)
    if not queue:
        return await interaction.response.send_message("대기열이 비어 있어요.", ephemeral=True)
    index -= 1
    if index < 0 or index >= len(queue):
        return await interaction.response.send_message("인덱스가 잘못되었습니다.", ephemeral=True)
    removed = queue[index]["title"]
    del queue[index]
    await update_panel(interaction.guild)
    save_state()
    await interaction.response.send_message(f"대기열에서 제거했습니다: {removed}", ephemeral=True)


@tree.command(name="search", description="유튜브에서 검색합니다.")
@app_commands.describe(query="검색어")
async def slash_search(interaction: discord.Interaction, query: str):
    role_err = check_role_interaction(interaction)
    if role_err:
        return await interaction.response.send_message(role_err, ephemeral=True)
    voice, err = enforce_voice_interaction(interaction, require_bot=False)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        results = await search_tracks(query, limit=5)
    except Exception as exc:
        return await interaction.followup.send(f"검색 실패: {exc}", ephemeral=True)
    search_cache[interaction.guild.id] = results
    embed = discord.Embed(title="검색 결과", description=f"`{query}`", color=0x57F287)
    for idx, item in enumerate(results[:5]):
        dur = format_duration(item.get("duration"))
        embed.add_field(name=f"{idx+1}. {item.get('title','제목 없음')}", value=f"길이: {dur}", inline=False)
    view = SearchView(interaction.guild.id, interaction.user.id, is_ephemeral=True)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@tree.command(name="choose", description="최근 검색 결과에서 선택해 대기열에 추가합니다.")
@app_commands.describe(index="선택할 번호(1부터)")
async def slash_choose(interaction: discord.Interaction, index: int):
    results = search_cache.get(interaction.guild.id)
    if not results:
        return await interaction.response.send_message("먼저 /search 로 검색해 주세요.", ephemeral=True)
    index -= 1
    if index < 0 or index >= len(results):
        return await interaction.response.send_message("인덱스가 잘못되었습니다.", ephemeral=True)

    cd_err = check_cooldown(interaction.user.id)
    if cd_err:
        return await interaction.response.send_message(cd_err, ephemeral=True)
    role_err = check_role_interaction(interaction)
    if role_err:
        return await interaction.response.send_message(role_err, ephemeral=True)
    voice, err = enforce_voice_interaction(interaction, require_bot=False)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if voice is None:
        voice = await interaction.user.voice.channel.connect()

    limit_err = check_queue_limits(interaction.guild.id, interaction.user.id)
    if limit_err:
        return await interaction.response.send_message(limit_err, ephemeral=True)

    track = results[index]
    queue = get_queue(interaction.guild.id)
    track = {
        "title": track.get("title"),
        "url": track.get("url"),
        "web_url": track.get("web_url"),
        "duration": track.get("duration"),
        "thumbnail": track.get("thumbnail"),
        "channel": interaction.channel,
        "channel_id": interaction.channel.id,
        "requester": interaction.user.display_name,
        "requester_id": interaction.user.id,
    }
    queue.append(track)

    if voice.is_playing() or voice.is_paused():
        await interaction.response.send_message(f"대기열에 추가: {track['title']}", ephemeral=True)
    else:
        await start_playback(interaction.guild, voice)
    await update_panel(interaction.guild, channel=interaction.channel)
    save_state()


# ---------- MapleStory Slash ----------


@tree.command(name="msbasic", description="메이플 기본 정보 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_msbasic(interaction: discord.Interaction, character_name: str):
    if not NEXON_API_KEY:
        return await interaction.response.send_message("NEXON_API_KEY가 설정되지 않았습니다.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        basic = await nexon_get("/maplestory/v1/character/basic", {"ocid": ocid})
        name = basic.get("character_name", character_name)
        world = basic.get("world_name", "?")
        level = basic.get("character_level", "?")
        job = basic.get("character_class", "?")
        gender = basic.get("character_gender", "?")
        guild = basic.get("character_guild_name") or "-"
        create = basic.get("character_date_create") or "-"
        desc = f"월드: {world}\n레벨: {level}\n직업: {job}\n성별: {gender}\n길드: {guild}\n생성일: {create}"
        embed = discord.Embed(title=f"{name} 기본 정보", description=desc, color=0x57F287)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="msstat", description="메이플 종합 능력치 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_msstat(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        stat = await nexon_get("/maplestory/v1/character/stat", {"ocid": ocid})
        latest = (stat.get("stat") or [])[:8]
        lines = [f"{s.get('stat_name')}: {s.get('stat_value')}" for s in latest]
        embed = discord.Embed(title=f"{character_name} 종합 능력치", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="mspop", description="메이플 인기도 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_mspop(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        pop = await nexon_get("/maplestory/v1/character/popularity", {"ocid": ocid})
        value = pop.get("popularity") or "?"
        embed = discord.Embed(title=f"{character_name} 인기도", description=f"인기도: {value}", color=0x57F287)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="msequip", description="메이플 장착 장비 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_msequip(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        eq = await nexon_get("/maplestory/v1/character/item-equipment", {"ocid": ocid})
        items = (eq.get("item_equipment") or [])[:10]
        lines = []
        for it in items:
            name = it.get("item_name") or "이름없음"
            star = it.get("starforce") or 0
            main = it.get("item_option", [])
            first_opt = main[0]["option_value"] if main else ""
            lines.append(f"{name} ★{star} {first_opt}")
        embed = discord.Embed(title=f"{character_name} 장착 장비 (상위 10)", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="msskill", description="메이플 스킬 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_msskill(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        skills = await nexon_get("/maplestory/v1/character/skill", {"ocid": ocid})
        list_skill = (skills.get("character_skill") or [])[:10]
        lines = [f"{s.get('skill_name')} Lv.{s.get('skill_level')}" for s in list_skill]
        embed = discord.Embed(title=f"{character_name} 스킬 (상위 10)", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="mslink", description="메이플 링크 스킬 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_mslink(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/link-skill", {"ocid": ocid})
        skills = (data.get("character_link_skill") or [])[:5]
        lines = [f"{s.get('skill_name')} Lv.{s.get('skill_level')}" for s in skills]
        embed = discord.Embed(title=f"{character_name} 링크 스킬", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="mspet", description="메이플 펫 정보 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_mspet(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/pet-equipment", {"ocid": ocid})
        pets = data.get("pet_equipment") or []
        lines = []
        for p in pets[:3]:
            lines.append(f"{p.get('pet_name')} | 장비: {p.get('pet_equipment_item_name') or '-'}")
        embed = discord.Embed(title=f"{character_name} 펫 정보", description="\n".join(lines) or "펫 없음", color=0x57F287)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="msandroid", description="메이플 안드로이드 정보 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_msandroid(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/android-equipment", {"ocid": ocid})
        android = data.get("android_name") or "-"
        hair = data.get("android_hair") or "-"
        face = data.get("android_face") or "-"
        embed = discord.Embed(title=f"{character_name} 안드로이드", color=0x57F287)
        embed.add_field(name="이름", value=android, inline=False)
        embed.add_field(name="헤어", value=hair, inline=True)
        embed.add_field(name="성형", value=face, inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="msbeauty", description="메이플 헤어/성형/피부 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_msbeauty(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/beauty-equipment", {"ocid": ocid})
        hair = data.get("character_hair") or "-"
        face = data.get("character_face") or "-"
        skin = data.get("character_skin_name") or "-"
        embed = discord.Embed(title=f"{character_name} 헤어/성형/피부", color=0x57F287)
        embed.add_field(name="헤어", value=hair, inline=False)
        embed.add_field(name="성형", value=face, inline=False)
        embed.add_field(name="피부", value=skin, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="msvmatrix", description="메이플 V매트릭스 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_msvmatrix(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/vmatrix", {"ocid": ocid})
        cores = (data.get("character_v_core_equipment") or [])[:6]
        lines = [f"{c.get('v_core_name')} Lv.{c.get('v_core_level')}" for c in cores]
        embed = discord.Embed(title=f"{character_name} V매트릭스", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="mshexa", description="메이플 HEXA 코어 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_mshexa(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/hexamatrix", {"ocid": ocid})
        skills = (data.get("character_hexacore_equipment") or [])[:6]
        lines = [f"{h.get('hexa_core_name')} Lv.{h.get('hexa_core_level')}" for h in skills]
        embed = discord.Embed(title=f"{character_name} HEXA 코어", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="mshexastat", description="메이플 HEXA 스탯 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_mshexastat(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/hexamatrix-stat", {"ocid": ocid})
        stats = data.get("character_hexamatrix_stat_core") or []
        lines = [f"{s.get('stat_core_name')} Lv.{s.get('stat_core_level')}" for s in stats[:5]]
        embed = discord.Embed(title=f"{character_name} HEXA 스탯", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="msdojo", description="메이플 무릉도장 기록 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_msdojo(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/dojang", {"ocid": ocid})
        floor = data.get("dojang_best_floor") or "?"
        rank = data.get("dojang_best_time_rank") or "?"
        time_val = data.get("dojang_best_time") or "?"
        embed = discord.Embed(title=f"{character_name} 무릉도장", color=0x57F287)
        embed.add_field(name="최고 층", value=floor, inline=True)
        embed.add_field(name="랭크", value=rank, inline=True)
        embed.add_field(name="기록", value=f"{time_val}초", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="msotherstat", description="메이플 기타 능력치 조회")
@app_commands.describe(character_name="캐릭터 이름")
async def slash_msotherstat(interaction: discord.Interaction, character_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ocid = await get_ocid(character_name)
        data = await nexon_get("/maplestory/v1/character/other-stat", {"ocid": ocid})
        stats = data.get("character_additional_information") or []
        lines = [f"{s.get('stat_name')}: {s.get('stat_value')}" for s in stats[:8]]
        embed = discord.Embed(title=f"{character_name} 기타 능력치", description="\n".join(lines) or "데이터 없음", color=0x57F287)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="msauc", description="메이플 경매장 시세 조회")
@app_commands.describe(item_name="아이템 이름")
async def slash_msauc(interaction: discord.Interaction, item_name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        params, clean = auction_params(item_name)
        data = await nexon_get("/maplestory/v1/auction", params)
        rows = sorted(data.get("items") or [], key=lambda x: x.get("unit_price", 0))[:5]
        lines = [f"{r.get('item_name')} | {r.get('unit_price')}메소 x{r.get('count',1)}" for r in rows]
        embed = discord.Embed(title=f"경매장 시세: {clean}", description="\n".join(lines) or "데이터 없음", color=0xFEE75C)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        msg = str(exc)
        if "OPENAPI00004" in msg or "valid parameter" in msg:
            await interaction.followup.send("조회 실패: 아이템명을 정확히 입력해 주세요. 예) /msauc 몽환의 벨트", ephemeral=True)
        else:
            await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


# ---------- FC Online Slash ----------


@tree.command(name="fcbasic", description="FC 온라인 기본 정보")
@app_commands.describe(nickname="닉네임")
async def slash_fcbasic(interaction: discord.Interaction, nickname: str):
    if not FIFA_API_KEY:
        return await interaction.response.send_message("FIFA_API_KEY가 설정되지 않았습니다.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        ouid = await fc_get_ouid(nickname)
        data = await fc_get("/fconline/v1/user/basic", {"ouid": ouid})
        level = data.get("level", "?")
        nickname = data.get("nickname", nickname)
        access = data.get("access_id", "-")
        desc = f"레벨: {level}\n닉네임: {nickname}\nAccess ID: {access}"
        embed = discord.Embed(title=f"{nickname} 기본 정보", description=desc, color=0x3498DB)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="fcmax", description="FC 역대 최고 등급")
@app_commands.describe(nickname="닉네임")
async def slash_fcmax(interaction: discord.Interaction, nickname: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ouid = await fc_get_ouid(nickname)
        data = await fc_get("/fconline/v1/user/maxdivision", {"ouid": ouid})
        latest = data.get("maxdivision") or []
        lines = [f"시즌:{d.get('seasonId')} | 등급:{d.get('division')} | 타입:{d.get('matchType')}" for d in latest[:5]]
        embed = discord.Embed(title=f"{nickname} 역대 최고 등급", description="\n".join(lines) or "데이터 없음", color=0x3498DB)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="fcmatch", description="FC 최근 경기 ID 조회")
@app_commands.describe(nickname="닉네임", matchtype="매치타입 (기본 50)")
async def slash_fcmatch(interaction: discord.Interaction, nickname: str, matchtype: str = "50"):
    await interaction.response.defer(ephemeral=True)
    try:
        ouid = await fc_get_ouid(nickname)
        params = {"ouid": ouid, "offset": 0, "limit": 5, "matchtype": matchtype}
        matches = await fc_get("/fconline/v1/user/match", params)
        ids = matches if isinstance(matches, list) else []
        lines = []
        for mid in ids[:5]:
            try:
                detail = await fc_get("/fconline/v1/match-detail", {"matchid": mid})
                infos = detail.get("matchInfo") or []
                if len(infos) < 2:
                    lines.append(f"{mid}: 상세 없음")
                    continue
                p1, p2 = infos[0], infos[1]
                mine = p1 if p1.get("ouid") == ouid else p2
                opp = p2 if mine is p1 else p1
                my_score = mine.get("shoot", {}).get("goalTotal") if mine else "?"
                opp_score = opp.get("shoot", {}).get("goalTotal") if opp else "?"
                opp_name = opp.get("nickname") if opp else "?"
                result = "무" if my_score == opp_score else ("승" if my_score > opp_score else "패")
                lines.append(f"{result} {my_score}:{opp_score} vs {opp_name} (matchId {mid})")
            except Exception as inner:
                lines.append(f"{mid}: 상세 실패 ({inner})")
        embed = discord.Embed(title=f"{nickname} 최근 경기 (최대 5)", description="\n".join(lines) or "데이터 없음", color=0x3498DB)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="fctrade", description="FC 최근 거래 조회")
@app_commands.describe(nickname="닉네임", tradetype="sell(판매)/buy(구매), 기본 sell")
async def slash_fctrade(interaction: discord.Interaction, nickname: str, tradetype: str = "sell"):
    await interaction.response.defer(ephemeral=True)
    try:
        nickname = nickname.strip()
        tmap = {"sell": "sell", "buy": "buy", "판매": "sell", "구매": "buy"}
        tval = tmap.get(tradetype.lower())
        if not tval:
            return await interaction.followup.send("tradetype은 sell(판매)/buy(구매) 중 하나", ephemeral=True)
        ouid = await fc_get_ouid(nickname)
        params = {"ouid": ouid, "tradetype": tval, "offset": 0, "limit": 5}
        try:
            data = await fc_get("/fconline/v1/user/trade", params)
        except ValueError as exc:
            if "OPENAPI00004" in str(exc):
                data = await fc_get("/fconline/v1/user/trade", {"ouid": ouid, "offset": 0, "limit": 5})
            else:
                raise
        rows = data.get("trades") if isinstance(data, dict) else data
        rows = rows or []
        lines = []
        for r in rows[:5]:
            item = fc_pretty_player_by_id(r.get("spid")) if r.get("spid") else "-"
            price = r.get("value") or "-"
            trade_type = r.get("tradeType") or tval
            date = r.get("tradeDate") or ""
            grade = r.get("grade") or "-"
            lines.append(f"{date} | {trade_type} | {item} | 강화:{grade} | 가격:{price}")
        embed = discord.Embed(
            title=f"{nickname} 거래 기록(최근 5, {tval})",
            description="\n".join(lines) or "데이터 없음",
            color=0x3498DB,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        msg = str(exc)
        if "OPENAPI00004" in msg:
            await interaction.followup.send("조회 실패: 닉네임을 확인하거나 거래 내역이 없는 경우일 수 있습니다. tradetype은 sell/buy만 지원하며, 없으면 자동 재시도합니다.", ephemeral=True)
        else:
            await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="fcmeta", description="FC 메타데이터 요약")
@app_commands.describe(meta_type="matchtype/season/division 중 하나")
async def slash_fcmeta(interaction: discord.Interaction, meta_type: str = "matchtype"):
    await interaction.response.defer(ephemeral=True)
    meta_type = meta_type.lower()
    endpoint_map = {
        "matchtype": "/static/fconline/meta/matchtype.json",
        "season": "/static/fconline/meta/seasonid.json",
        "division": "/static/fconline/meta/division.json",
    }
    endpoint = endpoint_map.get(meta_type)
    if not endpoint:
        return await interaction.followup.send("사용법: meta_type은 matchtype/season/division 중 하나", ephemeral=True)
    try:
        data = await fc_get(endpoint, {})
        if isinstance(data, list):
            items = data[:10]
            if meta_type == "matchtype":
                lines = [f"{d.get('matchtype')}: {d.get('desc')}" for d in items]
            elif meta_type == "season":
                lines = [f"{d.get('seasonId')}: {d.get('className')}" for d in items]
            else:
                lines = [f"{d.get('divisionId')}: {d.get('divisionName')}" for d in items]
        else:
            lines = ["데이터 없음"]
        embed = discord.Embed(title=f"FC 메타 ({meta_type})", description="\n".join(lines) or "데이터 없음", color=0x3498DB)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="fcmatchdetail", description="FC 최근 경기 결과 요약")
@app_commands.describe(nickname="닉네임", matchtype="매치타입 (기본 50)")
async def slash_fcmatchdetail(interaction: discord.Interaction, nickname: str, matchtype: str = "50"):
    await interaction.response.defer(ephemeral=True)
    try:
        ouid = await fc_get_ouid(nickname)
        params = {"ouid": ouid, "offset": 0, "limit": 5, "matchtype": matchtype}
        match_ids = await fc_get("/fconline/v1/user/match", params)
        match_ids = match_ids if isinstance(match_ids, list) else []
        lines = []
        for mid in match_ids[:5]:
            try:
                detail = await fc_get("/fconline/v1/match-detail", {"matchid": mid})
                infos = detail.get("matchInfo") or []
                if len(infos) < 2:
                    lines.append(f"{mid}: 상세 없음")
                    continue
                p1, p2 = infos[0], infos[1]
                mine = p1 if p1.get("ouid") == ouid else p2
                opp = p2 if mine is p1 else p1
                my_score = mine.get("shoot", {}).get("goalTotal") if mine else "?"
                opp_score = opp.get("shoot", {}).get("goalTotal") if opp else "?"
                opp_name = opp.get("nickname") if opp else "?"
                result = "무" if my_score == opp_score else ("승" if my_score > opp_score else "패")
                lines.append(f"{result} {my_score}:{opp_score} vs {opp_name} (matchId {mid})")
            except Exception as inner:
                lines.append(f"{mid}: 상세 실패 ({inner})")
        embed = discord.Embed(
            title=f"{nickname} 최근 경기 요약",
            description="\n".join(lines) or "데이터 없음",
            color=0x3498DB,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.command(name="fcplayer", description="FC 선수 이름으로 검색")
@app_commands.describe(name="선수 이름")
async def slash_fcplayer(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    try:
        await ensure_fc_meta()
        matches = find_players_by_name(name, limit=5)
        if not matches:
            return await interaction.followup.send("검색 결과가 없습니다.", ephemeral=True)
        lines = [fc_pretty_player(p) for p in matches]
        embed = discord.Embed(title=f"선수 검색: {name}", description="\n".join(lines), color=0x3498DB)
        first_spid = matches[0].get("id")
        if first_spid:
            embed.set_thumbnail(url=fc_player_image(first_spid))
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"조회 실패: {exc}", ephemeral=True)


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"오류가 발생했어요: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"오류가 발생했어요: {error}", ephemeral=True)
    except Exception:
        pass


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN 환경변수가 없습니다.")
    bot.run(token)
