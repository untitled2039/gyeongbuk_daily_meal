"""경북학숙 급식 디스코드 봇.

기능
- /급식 [날짜] [끼니]   예) /급식 날짜:내일 끼니:아침
- /아침 /점심 /저녁 [날짜]
- 봇 멘션 또는 DM으로 자연어:  "@급식봇 내일 아침", "10월 25일 저녁"
- 메시지 아래 버튼: 오늘 아침 / 오늘 점심 / 오늘 저녁 / 내일 급식
- 자동 알림 06:30 조식 / 11:30 중식 / 17:30 석식
    · /알림설정  : 지금 채널에 매일 알림 (서버 관리 권한 필요)
    · /알림해제  : 이 서버의 채널 알림 끄기
    · /알림구독  : 나에게 DM으로 알림,  /알림구독해제

환경변수: DISCORD_TOKEN (필수), PORT (선택: 지정하면 상태 확인용 웹페이지를 띄움)
"""
import asyncio
import json
import os
from datetime import date, time as dtime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

from meal import KST, find_meal, parse_utterance, today_kst

DATA_FILE = os.environ.get("BOT_DATA_FILE", "bot_data.json")
NOTIFY_TIMES = {
    dtime(6, 30, tzinfo=KST): "조식",
    dtime(11, 30, tzinfo=KST): "중식",
    dtime(17, 30, tzinfo=KST): "석식",
}
EMOJI = {"조식": "🌅", "중식": "☀️", "석식": "🌙"}
KOR = {"조식": "아침", "중식": "점심", "석식": "저녁"}


# ---------------- 설정 저장 ----------------
def load_data() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("channels", {})  # guild_id -> channel_id
    d.setdefault("dm_users", [])  # user_id 목록
    return d


def save_data(d: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


# ---------------- 식단 → 임베드 ----------------
async def get_meal(target: date):
    return await asyncio.to_thread(find_meal, target)


def meal_embed(target: date, info: dict | None, only=None) -> discord.Embed:
    wd = "월화수목금토일"[target.weekday()]
    label = "·".join(KOR[m] for m in only) if only else "식단"
    emb = discord.Embed(
        title=f"🍚 {target.month}월 {target.day}일({wd}) {label}",
        color=0xF4A261,
    )
    if not info or not info["meals"]:
        emb.description = "등록된 식단이 아직 없어요."
        return emb
    emb.url = info["url"]
    meals = info["meals"]
    if only and not any(m in meals for m in only):
        only = None  # 끼니 구분이 안 된 날은 전체 표시
    for name, items in meals.items():
        if only and name not in only:
            continue
        title = f"{EMOJI.get(name, '🍽')} {KOR.get(name, name)}"
        emb.add_field(name=title, value="\n".join(items) or "-", inline=bool(not only))
    emb.set_footer(text="경북학숙 주간메뉴")
    return emb


# ---------------- 버튼 (재시작 후에도 동작) ----------------
class MealButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _show(self, inter: discord.Interaction, offset: int, only):
        await inter.response.defer(ephemeral=True, thinking=True)
        target = today_kst() + timedelta(days=offset)
        await inter.followup.send(embed=meal_embed(target, await get_meal(target), only), ephemeral=True)

    @discord.ui.button(label="오늘 아침", emoji="🌅", custom_id="meal:0:조식", style=discord.ButtonStyle.secondary)
    async def b1(self, inter, _):
        await self._show(inter, 0, ["조식"])

    @discord.ui.button(label="오늘 점심", emoji="☀️", custom_id="meal:0:중식", style=discord.ButtonStyle.secondary)
    async def b2(self, inter, _):
        await self._show(inter, 0, ["중식"])

    @discord.ui.button(label="오늘 저녁", emoji="🌙", custom_id="meal:0:석식", style=discord.ButtonStyle.secondary)
    async def b3(self, inter, _):
        await self._show(inter, 0, ["석식"])

    @discord.ui.button(label="내일 급식", emoji="📅", custom_id="meal:1:all", style=discord.ButtonStyle.primary)
    async def b4(self, inter, _):
        await self._show(inter, 1, None)


# ---------------- 봇 ----------------
class MealBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())  # 특수 권한(인텐트) 필요 없음
        self.tree = app_commands.CommandTree(self)
        self.data = load_data()

    async def setup_hook(self):
        self.add_view(MealButtons())
        await self.tree.sync()
        self.notify_loop.start()
        self.prewarm_loop.start()
        if os.environ.get("PORT"):
            await start_web(int(os.environ["PORT"]))

    async def on_ready(self):
        print(f"[bot] 로그인: {self.user} / 서버 {len(self.guilds)}개", flush=True)

    # 멘션 또는 DM 자연어
    async def on_message(self, msg: discord.Message):
        if msg.author.bot:
            return
        is_dm = isinstance(msg.channel, discord.DMChannel)
        if not is_dm and self.user not in msg.mentions:
            return
        text = msg.content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "")
        target, only = parse_utterance(text)
        async with msg.channel.typing():
            info = await get_meal(target)
        await msg.reply(embed=meal_embed(target, info, only), view=MealButtons(), mention_author=False)

    # 자동 알림
    @tasks.loop(time=list(NOTIFY_TIMES))
    async def notify_loop(self):
        now = discord.utils.utcnow().astimezone(KST)
        meal_name = min(NOTIFY_TIMES.items(),
                        key=lambda kv: abs((now.hour * 60 + now.minute) - (kv[0].hour * 60 + kv[0].minute)))[1]
        target = today_kst()
        info = await get_meal(target)
        if not info or meal_name not in info["meals"]:
            print(f"[notify] {target} {meal_name} 식단 없음 → 건너뜀", flush=True)
            return
        emb = meal_embed(target, info, [meal_name])
        # 재배포 때 bot_data.json 이 지워지는 서버용: 환경변수로 고정 채널 지정 가능
        fixed = [c.strip() for c in os.environ.get("NOTIFY_CHANNEL_IDS", "").split(",") if c.strip()]
        targets = {str(c) for c in self.data["channels"].values()} | set(fixed)
        for cid in targets:
            ch = self.get_channel(int(cid))
            try:
                if ch:
                    await ch.send(embed=emb, view=MealButtons())
            except discord.HTTPException as e:
                print(f"[notify] 채널 {cid} 전송 실패: {e}", flush=True)
        for uid in list(self.data["dm_users"]):
            try:
                user = self.get_user(uid) or await self.fetch_user(uid)
                await user.send(embed=emb)
            except discord.HTTPException as e:
                print(f"[notify] DM {uid} 전송 실패: {e}", flush=True)
        print(f"[notify] {target} {meal_name} 전송 완료", flush=True)

    @notify_loop.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()

    # 오늘/내일 식단 미리 받아두기
    @tasks.loop(minutes=30)
    async def prewarm_loop(self):
        for off in (0, 1):
            try:
                await get_meal(today_kst() + timedelta(days=off))
            except Exception:
                pass


bot = MealBot()
MEAL_CHOICES = [
    app_commands.Choice(name="전체", value="all"),
    app_commands.Choice(name="아침", value="조식"),
    app_commands.Choice(name="점심", value="중식"),
    app_commands.Choice(name="저녁", value="석식"),
]


async def reply_meal(inter: discord.Interaction, date_text: str | None, only):
    await inter.response.defer(thinking=True)
    target, _ = parse_utterance(date_text or "오늘")
    info = await get_meal(target)
    await inter.followup.send(embed=meal_embed(target, info, only), view=MealButtons())


@bot.tree.command(name="급식", description="급식 메뉴를 보여줘요")
@app_commands.describe(날짜="오늘, 내일, 모레, 어제, 10월 25일 …", 끼니="아침/점심/저녁 (비우면 전체)")
@app_commands.choices(끼니=MEAL_CHOICES)
async def cmd_meal(inter: discord.Interaction, 날짜: Optional[str] = None,
                   끼니: Optional[app_commands.Choice[str]] = None):
    only = None if (끼니 is None or 끼니.value == "all") else [끼니.value]
    await reply_meal(inter, 날짜, only)


def _make_single(name: str, meal_key: str):
    @bot.tree.command(name=name, description=f"{name} 메뉴만 보여줘요")
    @app_commands.describe(날짜="오늘, 내일, 10월 25일 … (비우면 오늘)")
    async def _cmd(inter: discord.Interaction, 날짜: Optional[str] = None):
        await reply_meal(inter, 날짜, [meal_key])


_make_single("아침", "조식")
_make_single("점심", "중식")
_make_single("저녁", "석식")


@bot.tree.command(name="알림설정", description="이 채널에 매일 06:30/11:30/17:30 식단 알림을 보내요")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def cmd_notify_on(inter: discord.Interaction):
    bot.data["channels"][str(inter.guild_id)] = inter.channel_id
    save_data(bot.data)
    await inter.response.send_message(
        f"✅ 이제 {inter.channel.mention} 에 매일 🌅06:30 아침 · ☀️11:30 점심 · 🌙17:30 저녁 메뉴를 보낼게요.")


@bot.tree.command(name="알림해제", description="이 서버의 식단 알림을 꺼요")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def cmd_notify_off(inter: discord.Interaction):
    removed = bot.data["channels"].pop(str(inter.guild_id), None)
    save_data(bot.data)
    await inter.response.send_message("🔕 알림을 껐어요." if removed else "설정된 알림이 없어요.")


@bot.tree.command(name="알림구독", description="나에게 DM으로 매일 식단 알림을 보내요")
async def cmd_dm_on(inter: discord.Interaction):
    if inter.user.id not in bot.data["dm_users"]:
        bot.data["dm_users"].append(inter.user.id)
        save_data(bot.data)
    await inter.response.send_message("✅ 매일 06:30 · 11:30 · 17:30 에 DM으로 보내드릴게요. "
                                      "(서버 설정에서 DM 허용이 켜져 있어야 해요)", ephemeral=True)


@bot.tree.command(name="알림구독해제", description="DM 식단 알림을 꺼요")
async def cmd_dm_off(inter: discord.Interaction):
    if inter.user.id in bot.data["dm_users"]:
        bot.data["dm_users"].remove(inter.user.id)
        save_data(bot.data)
    await inter.response.send_message("🔕 DM 알림을 껐어요.", ephemeral=True)


# ---------------- (선택) 상태 확인 웹페이지 ----------------
async def start_web(port: int):
    """Render 무료 웹서비스 + 외부 핑으로 24시간 켜두고 싶을 때 사용."""
    from aiohttp import web  # discord.py 설치 시 함께 설치됨

    async def health(_):
        target = today_kst()
        info = await get_meal(target)
        lines = [f"OK - {bot.user}", f"{target} 식단: {'있음' if info else '없음'}"]
        return web.Response(text="\n".join(lines))

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"[web] 상태 페이지 :{port}", flush=True)


if __name__ == "__main__":
    bot.run(os.environ["DISCORD_TOKEN"])
