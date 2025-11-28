import os
import asyncio
from collections import deque

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

# yt-dlp 설정 (고음질 우선, 검색 허용)
ytdl_opts = {
    "format": "bestaudio[ext=webm][abr>=160]/bestaudio[abr>=128]/bestaudio/best",
    "quiet": True,
    "nocheckcertificate": True,
    "noplaylist": True,
    "default_search": "ytsearch",
}
ytdl = yt_dlp.YoutubeDL(ytdl_opts)

# 길드별 재생 대기열과 검색 캐시
queues: dict[int, deque] = {}
search_cache: dict[int, list[dict]] = {}
current_track: dict[int, dict | None] = {}
panels: dict[int, discord.Message] = {}


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


@bot.command(name="미개")
async def mi_gae(ctx):
    await ctx.send("저는 미개한 김규민입니다")


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
        return info.get("title", "제목 없음"), stream_url
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
            results.append({"title": title, "url": url})
    if not results:
        raise ValueError("검색 결과가 없습니다.")
    return results


def build_panel_embed(guild: discord.Guild) -> discord.Embed:
    voice = guild.voice_client
    track = current_track.get(guild.id)
    qlen = len(get_queue(guild.id))

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
        url = track.get("url")
        requester = track.get("requester", "알 수 없음")
        if url:
            desc = f"[{title}]({url})\n요청자: {requester}"
        else:
            desc = f"{title}\n요청자: {requester}"

    embed = discord.Embed(title="음악 패널", description=desc, color=0x5865F2)
    embed.add_field(name="상태", value=status, inline=True)
    embed.add_field(name="대기열", value=f"{qlen} 곡", inline=True)
    if voice and voice.channel:
        embed.add_field(name="음성 채널", value=voice.channel.name, inline=True)
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
    title = track["title"]
    stream_url = track["url"]
    channel = track["channel"]
    current_track[guild.id] = track

    ffmpeg_opts = {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": "-vn -ac 2 -ar 48000 -b:a 160k",
    }
    source = discord.FFmpegOpusAudio(stream_url, **ffmpeg_opts)

    def after_playback(error):
        bot.loop.call_soon_threadsafe(asyncio.create_task, handle_after(guild, error))

    voice.play(source, after=after_playback)
    try:
        await channel.send(f"재생 시작: {title}")
    except Exception:
        pass
    await update_panel(guild, channel=channel)


async def handle_after(guild: discord.Guild, error: Exception | None):
    voice = guild.voice_client
    if error and voice:
        try:
            # 마지막에 재생한 채널 정보를 알 수 없으므로 길드 기본 시스템 채널이 있으면 거기로 보냄
            channel = guild.system_channel
            if channel:
                await channel.send(f"재생 중 오류: {error}")
        except Exception:
            pass
    if voice and not voice.is_playing() and not voice.is_paused():
        await start_playback(guild, voice)
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
    voice, err = enforce_voice_ctx(ctx, require_bot=True)
    if err:
        return await ctx.send(err)
    await voice.disconnect()
    get_queue(ctx.guild.id).clear()
    clear_search(ctx.guild.id)
    current_track[ctx.guild.id] = None
    await update_panel(ctx.guild)
    await ctx.send("음성 채널 연결을 끊었습니다.")


@bot.command()
async def play(ctx, *, url: str):
    try:
        voice, err = enforce_voice_ctx(ctx, require_bot=False)
        if err:
            return await ctx.send(err)
        if voice is None:
            voice = await ctx.author.voice.channel.connect()

        title, stream_url = await extract_stream(url)
        queue = get_queue(ctx.guild.id)
        queue.append({"title": title, "url": stream_url, "channel": ctx.channel, "requester": ctx.author.display_name})

        if voice.is_playing() or voice.is_paused():
            await ctx.send(f"대기열에 추가: {title}")
        else:
            await start_playback(ctx.guild, voice)
        await update_panel(ctx.guild, channel=ctx.channel)
        clear_search(ctx.guild.id)
    except Exception as exc:
        return await ctx.send(f"재생에 실패했습니다: {exc}")


@bot.command()
async def stop(ctx):
    voice, err = enforce_voice_ctx(ctx, require_bot=True)
    if err:
        return await ctx.send(err)
    get_queue(ctx.guild.id).clear()
    clear_search(ctx.guild.id)
    current_track[ctx.guild.id] = None
    voice.stop()
    await update_panel(ctx.guild)
    await ctx.send("재생을 중지했어요.")


@bot.command()
async def pause(ctx):
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
    voice, err = enforce_voice_ctx(ctx, require_bot=True)
    if err:
        return await ctx.send(err)
    queue = get_queue(ctx.guild.id)
    queue.clear()
    await update_panel(ctx.guild)
    await ctx.send("대기열을 비웠습니다.")


@bot.command(name="panel")
async def panel_cmd(ctx):
    voice, err = enforce_voice_ctx(ctx, require_bot=False)
    if err:
        return await ctx.send(err)
    await update_panel(ctx.guild, channel=ctx.channel)
    await ctx.send("패널을 생성/업데이트했습니다.", delete_after=5)


@bot.command(name="move")
async def queue_move(ctx, src: int, dst: int):
    """대기열에서 src번째 트랙을 dst 위치로 이동 (1-based index)."""
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
    await ctx.send("순서를 변경했습니다.")


@bot.command(name="remove")
async def queue_remove(ctx, index: int):
    """대기열에서 index번째 트랙을 제거 (1-based index)."""
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
    await ctx.send(f"대기열에서 제거했습니다: {removed}")


@bot.command(name="search")
async def search_cmd(ctx, *, query: str):
    voice, err = enforce_voice_ctx(ctx, require_bot=False)
    if err:
        return await ctx.send(err)
    try:
        results = await search_tracks(query, limit=5)
    except Exception as exc:
        return await ctx.send(f"검색 실패: {exc}")
    search_cache[ctx.guild.id] = results
    lines = [f"{idx+1}. {item['title']}" for idx, item in enumerate(results)]
    await ctx.send("검색 결과:\n" + "\n".join(lines) + "\n!choose 숫자로 선택하세요.")


@bot.command(name="choose")
async def choose_cmd(ctx, index: int):
    results = search_cache.get(ctx.guild.id)
    if not results:
        return await ctx.send("먼저 !search 로 검색해 주세요.")
    index -= 1
    if index < 0 or index >= len(results):
        return await ctx.send("인덱스가 잘못되었습니다.")

    voice, err = enforce_voice_ctx(ctx, require_bot=False)
    if err:
        return await ctx.send(err)
    if voice is None:
        voice = await ctx.author.voice.channel.connect()

    track = results[index]
    queue = get_queue(ctx.guild.id)
    queue.append({"title": track["title"], "url": track["url"], "channel": ctx.channel, "requester": ctx.author.display_name})

    if voice.is_playing() or voice.is_paused():
        await ctx.send(f"대기열에 추가: {track['title']}")
    else:
        await start_playback(ctx.guild, voice)
    await update_panel(ctx.guild, channel=ctx.channel)


@tree.command(name="join", description="현재 음성 채널에 봇을 초대합니다.")
async def slash_join(interaction: discord.Interaction):
    if interaction.user.voice is None:
        return await interaction.response.send_message("먼저 음성 채널에 들어가 주세요.", ephemeral=True)
    await interaction.user.voice.channel.connect()
    await interaction.response.send_message(f"{interaction.user.voice.channel.name} 채널에 연결했어요.", ephemeral=True)


@tree.command(name="leave", description="봇을 음성 채널에서 내보냅니다.")
async def slash_leave(interaction: discord.Interaction):
    if interaction.guild.voice_client is None:
        return await interaction.response.send_message("현재 연결된 음성 채널이 없어요.", ephemeral=True)
    await interaction.guild.voice_client.disconnect()
    get_queue(interaction.guild.id).clear()
    clear_search(interaction.guild.id)
    current_track[interaction.guild.id] = None
    await update_panel(interaction.guild)
    await interaction.response.send_message("음성 채널 연결을 끊었습니다.", ephemeral=True)


@tree.command(name="play", description="유튜브 링크의 오디오를 재생합니다.")
@app_commands.describe(url="유튜브 주소")
async def slash_play(interaction: discord.Interaction, url: str):
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True)

    try:
        voice, err = enforce_voice_interaction(interaction, require_bot=False)
        if err:
            return await interaction.followup.send(err, ephemeral=True)
        if voice is None:
            voice = await interaction.user.voice.channel.connect()

        title, stream_url = await extract_stream(url)
        queue = get_queue(interaction.guild.id)
        queue.append({"title": title, "url": stream_url, "channel": interaction.channel, "requester": interaction.user.display_name})

        if voice.is_playing() or voice.is_paused():
            await interaction.followup.send(f"대기열에 추가: {title}", ephemeral=True)
        else:
            await start_playback(interaction.guild, voice)
        await update_panel(interaction.guild, channel=interaction.channel)
        clear_search(interaction.guild.id)
    except Exception as exc:
        try:
            await interaction.followup.send(f"재생에 실패했습니다: {exc}", ephemeral=True)
        except Exception:
            pass


@tree.command(name="stop", description="재생 중인 오디오를 중지합니다.")
async def slash_stop(interaction: discord.Interaction):
    voice, err = enforce_voice_interaction(interaction, require_bot=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    get_queue(interaction.guild.id).clear()
    clear_search(interaction.guild.id)
    current_track[interaction.guild.id] = None
    voice.stop()
    await update_panel(interaction.guild)
    await interaction.response.send_message("재생을 중지했어요.", ephemeral=True)


@tree.command(name="pause", description="재생을 일시정지합니다.")
async def slash_pause(interaction: discord.Interaction):
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
    voice, err = enforce_voice_interaction(interaction, require_bot=True)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    queue = get_queue(interaction.guild.id)
    queue.clear()
    clear_search(interaction.guild.id)
    await update_panel(interaction.guild)
    await interaction.response.send_message("대기열을 비웠습니다.", ephemeral=True)


@tree.command(name="panel", description="음악 패널을 표시합니다.")
async def slash_panel(interaction: discord.Interaction):
    voice, err = enforce_voice_interaction(interaction, require_bot=False)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    await update_panel(interaction.guild, channel=interaction.channel)
    await interaction.response.send_message("패널을 생성/업데이트했습니다.", ephemeral=True)


@tree.command(name="move", description="대기열 트랙 순서를 변경합니다.")
@app_commands.describe(src="이동할 트랙 번호(1부터)", dst="옮길 위치(1부터)")
async def slash_move(interaction: discord.Interaction, src: int, dst: int):
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
    await interaction.response.send_message("순서를 변경했습니다.", ephemeral=True)


@tree.command(name="remove", description="대기열에서 특정 트랙을 제거합니다.")
@app_commands.describe(index="제거할 트랙 번호(1부터)")
async def slash_remove(interaction: discord.Interaction, index: int):
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
    await interaction.response.send_message(f"대기열에서 제거했습니다: {removed}", ephemeral=True)


@tree.command(name="search", description="유튜브에서 검색합니다.")
@app_commands.describe(query="검색어")
async def slash_search(interaction: discord.Interaction, query: str):
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
    lines = [f"{idx+1}. {item['title']}" for idx, item in enumerate(results)]
    await interaction.followup.send("검색 결과:\n" + "\n".join(lines) + "\n/choose 숫자로 선택하세요.", ephemeral=True)


@tree.command(name="choose", description="최근 검색 결과에서 선택해 대기열에 추가합니다.")
@app_commands.describe(index="선택할 번호(1부터)")
async def slash_choose(interaction: discord.Interaction, index: int):
    results = search_cache.get(interaction.guild.id)
    if not results:
        return await interaction.response.send_message("먼저 /search 로 검색해 주세요.", ephemeral=True)
    index -= 1
    if index < 0 or index >= len(results):
        return await interaction.response.send_message("인덱스가 잘못되었습니다.", ephemeral=True)

    voice, err = enforce_voice_interaction(interaction, require_bot=False)
    if err:
        return await interaction.response.send_message(err, ephemeral=True)
    if voice is None:
        voice = await interaction.user.voice.channel.connect()

    track = results[index]
    queue = get_queue(interaction.guild.id)
    queue.append({"title": track["title"], "url": track["url"], "channel": interaction.channel, "requester": interaction.user.display_name})

    if voice.is_playing() or voice.is_paused():
        await interaction.response.send_message(f"대기열에 추가: {track['title']}", ephemeral=True)
    else:
        await start_playback(interaction.guild, voice)
    await update_panel(interaction.guild, channel=interaction.channel)


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
