# -*- coding: utf-8 -*-
"""
Telegram → Discord Voice  (เวอร์ชัน 3 กะ)
------------------------------------------
บอทฟังกลุ่ม Telegram ด้วยบัญชีเราเอง (ไม่ต้องเอาบอทเข้ากลุ่ม)
แต่ละ "กะ" ตั้งได้เอง (จากหน้าเว็บ):
    - คำที่จับ (keyword)  → ถ้าเจอคำนี้ในข้อความกลุ่ม
    - ห้องเสียง           → บอทเข้าห้องนี้
    - ข้อความที่ให้พูด     → อ่านข้อความนี้ (ไม่ใช่ข้อความในกลุ่ม)
พูดจบแล้ว "ออกจากห้อง" ทันที

ค่าทั้งหมดเก็บใน Supabase settings (key = tts_voice_config)

Environment Variables (Railway → Variables):
    TG_API_ID, TG_API_HASH, TG_SESSION, TARGET_GROUPS
    DISCORD_TOKEN, SUPABASE_URL, SUPABASE_KEY
"""

import asyncio
import ctypes.util
import json
import os
import shutil
import sys
import tempfile

import discord
import edge_tts
import requests
from discord.ext import tasks
from telethon import TelegramClient, events
from telethon.sessions import StringSession


# ---------- หา ffmpeg ----------
def _find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"

FFMPEG_EXE = _find_ffmpeg()


# ---------- โหลด libopus ----------
def _load_opus():
    if discord.opus.is_loaded():
        return True
    candidates = ["libopus.so.0", "libopus.so", "opus",
                  "/usr/lib/x86_64-linux-gnu/libopus.so.0", "/usr/lib/libopus.so.0"]
    try:
        f = ctypes.util.find_library("opus")
        if f:
            candidates.insert(0, f)
    except Exception:
        pass
    for name in candidates:
        try:
            discord.opus.load_opus(name)
            if discord.opus.is_loaded():
                print(f"✅ โหลด libopus จาก: {name}", flush=True)
                return True
        except Exception:
            continue
    print("❌ โหลด libopus ไม่ได้", flush=True)
    return False


# ---------- ENV ----------
TG_API_ID   = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH = os.environ.get("TG_API_HASH", "")
TG_SESSION  = os.environ.get("TG_SESSION", "")
TARGET_GROUPS = [g.strip() for g in os.environ.get("TARGET_GROUPS", "").split(",") if g.strip()]
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

_required = {
    "TG_API_ID": TG_API_ID, "TG_API_HASH": TG_API_HASH, "TG_SESSION": TG_SESSION,
    "TARGET_GROUPS": TARGET_GROUPS, "DISCORD_TOKEN": DISCORD_TOKEN,
    "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY,
}
_missing = [k for k, v in _required.items() if not v]
if _missing:
    print(f"❌ ยังไม่ได้ตั้งค่า: {', '.join(_missing)}", flush=True)
    sys.exit(1)


# ---------- state ----------
tg_client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)
intents = discord.Intents.default()
intents.voice_states = True
bot = discord.Client(intents=intents)

speak_queue: "asyncio.Queue[dict]" = asyncio.Queue()
_tg_targets: dict[int, str] = {}

# ค่าจากเว็บ
cfg = {
    "speech_rate": "-15%",   # ความเร็วเสียง (ติดลบ = ช้าลง)
    "telegram_groups": [],   # กลุ่ม Telegram ที่จะฟัง (ตั้งจากเว็บ) ว่าง = ใช้ค่าจาก ENV
    "shifts": [],            # [{name, enabled, keyword, voice_channel_ids[], voice_name, announce_text, repeat}]
}


def fetch_config():
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/settings",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            params={"key": "eq.tts_voice_config", "select": "value"},
            timeout=10,
        )
        if r.status_code == 200 and r.json():
            raw = r.json()[0].get("value")
            return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception as e:
        print(f"อ่าน config ไม่ได้: {e}", flush=True)
    return None


@tasks.loop(seconds=8)
async def poll_config():
    new = fetch_config()
    if new is not None:
        cfg.update(new)


# ---------- speak worker: เข้าห้อง → พูด → ออก ----------
async def speak_worker():
    while True:
        job = await speak_queue.get()   # {room_id, text, voice_name}
        vc = None
        mp3 = None
        try:
            room_id = int(job["room_id"])
            text = (job.get("text") or "").strip()
            voice = job.get("voice_name") or "th-TH-PremwadeeNeural"
            rate = cfg.get("speech_rate") or "-15%"
            if not text:
                continue

            ch = bot.get_channel(room_id)
            if ch is None:
                try:
                    ch = await bot.fetch_channel(room_id)
                except Exception as e:
                    print(f"หาห้องไม่เจอ {room_id}: {e}", flush=True)
                    continue
            if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                print("❌ ไม่ใช่ห้องเสียง", flush=True)
                continue

            # เข้าห้อง (ถ้าค้างอยู่ห้องอื่นให้ย้าย)
            existing = bot.voice_clients[0] if bot.voice_clients else None
            if existing and existing.is_connected():
                vc = existing
                if vc.channel.id != room_id:
                    await vc.move_to(ch)
            else:
                vc = await ch.connect(reconnect=True, self_deaf=True)
            print(f"✅ เข้าห้อง: {ch.name}", flush=True)

            # สร้างเสียง (พูดช้าลงตาม rate)
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                mp3 = f.name
            try:
                await edge_tts.Communicate(text, voice, rate=rate).save(mp3)
            except Exception as e:
                print(f"❌ สร้างเสียงไม่ได้: {type(e).__name__}: {e!r}", flush=True)
                continue

            if not os.path.exists(mp3) or os.path.getsize(mp3) == 0:
                print("❌ ไฟล์เสียงว่าง", flush=True)
                continue

            # เล่นซ้ำตามจำนวนรอบ
            repeat = int(job.get("repeat") or 1)
            if repeat < 1:
                repeat = 1
            for n in range(repeat):
                while vc.is_playing():
                    await asyncio.sleep(0.3)
                done = asyncio.Event()
                perr = {}
                def _after(err):
                    if err:
                        perr["e"] = err
                    bot.loop.call_soon_threadsafe(done.set)
                vc.play(discord.FFmpegPCMAudio(mp3, executable=FFMPEG_EXE), after=_after)
                await done.wait()
                if perr.get("e"):
                    print(f"❌ เล่นเสียงไม่ได้: {perr['e']!r}", flush=True)
                    break
                else:
                    print(f"🔊 พูดสำเร็จ (รอบ {n + 1}/{repeat})", flush=True)
                if n < repeat - 1:
                    await asyncio.sleep(0.8)  # เว้นจังหวะสั้นๆ ระหว่างรอบ

        except Exception as e:
            print(f"speak error: {type(e).__name__}: {e!r}", flush=True)
        finally:
            # ออกจากห้องเสมอเมื่อพูดจบ
            try:
                if vc and vc.is_connected():
                    await vc.disconnect(force=True)
                    print("👋 ออกจากห้องแล้ว", flush=True)
            except Exception:
                pass
            if mp3 and os.path.exists(mp3):
                try:
                    os.remove(mp3)
                except Exception:
                    pass
            speak_queue.task_done()


# ---------- discord ----------
@bot.event
async def on_ready():
    print(f"✅ Discord bot ล็อกอิน: {bot.user}", flush=True)
    print(f"🎧 ffmpeg: {FFMPEG_EXE}", flush=True)
    _load_opus()
    if not getattr(bot, "_started", False):
        bot._started = True
        bot.loop.create_task(speak_worker())
        poll_config.start()


# ---------- telegram ----------
def _target_group_names():
    """กลุ่มที่จะฟัง: ใช้จากเว็บก่อน (cfg.telegram_groups) ถ้าว่างใช้จาก ENV"""
    web = cfg.get("telegram_groups") or []
    web = [str(g).strip() for g in web if str(g).strip()]
    return web if web else TARGET_GROUPS


async def start_telegram():
    await tg_client.start()
    me = await tg_client.get_me()
    print(f"✅ Telegram ล็อกอิน: {me.first_name}", flush=True)
    print(f"👂 กลุ่มเริ่มต้น: {', '.join(_target_group_names()) or '(ยังไม่ตั้ง)'}", flush=True)

    # ฟัง "ทุกข้อความ" แล้วค่อยกรองด้วยชื่อกลุ่ม → เปลี่ยนกลุ่มจากเว็บได้ทันที ไม่ต้อง deploy ใหม่
    @tg_client.on(events.NewMessage())
    async def handler(event):
        raw = (event.raw_text or "").strip()
        if not raw:
            return

        # เอาเฉพาะกลุ่ม/แชนแนล
        if not (event.is_group or event.is_channel):
            return

        # กรองด้วยชื่อกลุ่ม
        try:
            chat = await event.get_chat()
            title = getattr(chat, "title", "") or ""
        except Exception:
            title = ""
        wanted = _target_group_names()
        if wanted and not any(g in title for g in wanted):
            return

        shifts = cfg.get("shifts") or []
        for sh in shifts:
            if not sh.get("enabled"):
                continue
            kw = (sh.get("keyword") or "").strip()
            text = (sh.get("announce_text") or "").strip()
            rooms = sh.get("voice_channel_ids")
            if not rooms:
                one = sh.get("voice_channel_id")
                rooms = [one] if one else []
            rooms = [r for r in rooms if r]
            if not (kw and rooms and text):
                continue
            if kw in raw:
                repeat = int(sh.get("repeat") or 1)
                if repeat < 1:
                    repeat = 1
                print(f"[ตรงคำ] กลุ่ม='{title}' กะ='{sh.get('name')}' คำ='{kw}' ห้อง={len(rooms)} รอบ={repeat}", flush=True)
                for room in rooms:
                    await speak_queue.put({
                        "room_id": room,
                        "text": text,
                        "voice_name": sh.get("voice_name"),
                        "repeat": repeat,
                    })

    print("✅ พร้อมฟังข้อความแล้ว (กรองตามชื่อกลุ่มที่ตั้งในเว็บ)", flush=True)


async def main():
    await start_telegram()
    await asyncio.gather(
        bot.start(DISCORD_TOKEN),
        tg_client.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
