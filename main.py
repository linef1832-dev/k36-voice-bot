# -*- coding: utf-8 -*-
"""
Telegram → Discord Voice  (เวอร์ชันควบคุมผ่านเว็บ)
--------------------------------------------------
บอทอ่าน "ห้องเสียง / เปิด-ปิด / คำกรอง / เสียง" จาก Supabase ตาราง settings
(key = tts_voice_config) ซึ่งหน้าเว็บ "ศูนย์ควบคุมเสียง" เป็นคนเขียนค่านั้น
=> ผู้ใช้แค่ติ๊กเลือกในเว็บ ไม่ต้องมายุ่งกับ Railway อีก

Environment Variables ที่ต้องตั้งใน Railway → Variables:
    TG_API_ID, TG_API_HASH, TG_SESSION, TARGET_GROUPS
    DISCORD_TOKEN
    SUPABASE_URL   = https://xxxx.supabase.co
    SUPABASE_KEY   = service_role key
(ไม่ต้องใส่ channel id แล้ว — คุมจากเว็บ)
"""

import asyncio
import json
import os
import re
import sys
import tempfile

import shutil

import discord
import edge_tts

# หา ffmpeg: ใช้ของระบบก่อน ถ้าไม่มีใช้ตัวที่ฝังมากับ pip (imageio-ffmpeg)
def _find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"  # ปล่อยให้ discord ลองเอง

FFMPEG_EXE = _find_ffmpeg()


# โหลด libopus (จำเป็นสำหรับส่งเสียงเข้า Discord voice)
def _load_opus():
    if discord.opus.is_loaded():
        return True
    # ลองชื่อ/พาธที่พบบ่อยบน Linux
    candidates = [
        "libopus.so.0", "libopus.so", "opus",
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/libopus.so.0",
        "/nix/var/nix/profiles/default/lib/libopus.so.0",
    ]
    # ลองจาก ctypes.util ด้วย
    try:
        import ctypes.util
        found = ctypes.util.find_library("opus")
        if found:
            candidates.insert(0, found)
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
    print("❌ โหลด libopus ไม่ได้ — เสียงจะไม่ออก (ต้องติดตั้ง libopus บนเซิร์ฟเวอร์)", flush=True)
    return False
import requests
from discord.ext import tasks
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ---------------- ENV ----------------
TG_API_ID   = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH = os.environ.get("TG_API_HASH", "")
TG_SESSION  = os.environ.get("TG_SESSION", "")
TARGET_GROUPS = [g.strip() for g in os.environ.get("TARGET_GROUPS", "").split(",") if g.strip()]
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
MAX_CHARS   = int(os.environ.get("MAX_CHARS", "400"))

_required = {
    "TG_API_ID": TG_API_ID, "TG_API_HASH": TG_API_HASH, "TG_SESSION": TG_SESSION,
    "TARGET_GROUPS": TARGET_GROUPS, "DISCORD_TOKEN": DISCORD_TOKEN,
    "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY,
}
_missing = [k for k, v in _required.items() if not v]
if _missing:
    print(f"❌ ยังไม่ได้ตั้งค่า: {', '.join(_missing)} (Railway → Variables)", flush=True)
    sys.exit(1)

# ---------------- state ----------------
tg_client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)
intents = discord.Intents.default()
intents.voice_states = True
bot = discord.Client(intents=intents)

speak_queue: "asyncio.Queue[str]" = asyncio.Queue()
_tg_targets: dict[int, str] = {}

# ค่าจากเว็บ (default = ปิด)
cfg = {
    "enabled": False,
    "voice_channel_id": "",
    "keyword_filter": "",
    "voice_name": "th-TH-PremwadeeNeural",
}


# ---------------- helper ----------------
def clean_text(text: str) -> str:
    text = re.sub(r"https?://\S+", " มีลิงก์แนบมา ", text)
    text = re.sub(r"[^\u0E00-\u0E7Fa-zA-Z0-9\s.,!?%:()\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_CHARS]


def build_announcement(sender: str, raw: str) -> str:
    body = clean_text(raw)
    if not body:
        return ""
    tm = re.search(r"(\d{1,2})[:.](\d{2})\s*[-–]\s*(\d{1,2})[:.](\d{2})", raw)
    if "เช็คชื่อ" in raw and tm:
        return f"แจ้งเตือน ถึงเวลาเช็คชื่อ รอบเวลา {tm.group(0).replace('.', ':')}. {body}"
    return f"ข้อความใหม่จาก {sender}. {body}"


def fetch_config():
    """อ่าน tts_voice_config จาก Supabase (settings เก็บ value เป็น JSON string)"""
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


# ---------------- voice ----------------
async def current_vc() -> discord.VoiceClient | None:
    return bot.voice_clients[0] if bot.voice_clients else None


async def reconcile_voice():
    """ปรับสถานะห้องเสียงให้ตรงกับ cfg"""
    vc = await current_vc()
    want = cfg.get("enabled") and cfg.get("voice_channel_id")

    if not want:
        if vc and vc.is_connected():
            await vc.disconnect(force=True)
            print("⏸️ ปิดเสียง — ออกจากห้อง", flush=True)
        return

    target_id = int(cfg["voice_channel_id"])

    # ถ้าต่ออยู่แล้วในห้องที่ถูกต้อง → ไม่ต้องทำอะไร (เงียบ)
    if vc and vc.is_connected() and vc.channel and vc.channel.id == target_id:
        return

    ch = bot.get_channel(target_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(target_id)
        except Exception as e:
            print(f"หาห้องไม่เจอ {target_id}: {e}", flush=True)
            return
    if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
        print("❌ id ที่เลือกไม่ใช่ห้องเสียง", flush=True)
        return

    try:
        if vc and vc.is_connected():
            await vc.move_to(ch)
            print(f"➡️ ย้ายไปห้อง: {ch.name}", flush=True)
        else:
            await ch.connect(reconnect=True, self_deaf=True)
            print(f"✅ เข้าห้องเสียง: {ch.name}", flush=True)
    except discord.ClientException:
        # ต่ออยู่แล้ว (แข่งกันหลายรอบ) — ข้ามเงียบๆ
        pass


@tasks.loop(seconds=8)
async def poll_config():
    new = fetch_config()
    if new is not None:
        changed = new != {k: cfg.get(k) for k in new}
        cfg.update(new)
        if changed:
            print(f"🔄 config อัปเดต: enabled={cfg.get('enabled')} room={cfg.get('voice_channel_id')}", flush=True)
    try:
        await reconcile_voice()
    except Exception as e:
        print(f"reconcile error: {e}", flush=True)


# ---------------- speak worker ----------------
async def speak_worker():
    while True:
        text = await speak_queue.get()
        mp3 = None
        try:
            # ถ้าเปิดใช้งานอยู่แต่หลุดห้อง → ลองต่อกลับก่อน
            vc = await current_vc()
            if not (vc and vc.is_connected()):
                await reconcile_voice()
                vc = await current_vc()
            if not (vc and vc.is_connected()):
                print("⚠️ ยังไม่ได้อยู่ในห้องเสียง ข้ามข้อความนี้", flush=True)
                continue

            # 1) สร้างไฟล์เสียง
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                mp3 = f.name
            try:
                await edge_tts.Communicate(text, cfg.get("voice_name") or "th-TH-PremwadeeNeural").save(mp3)
            except Exception as e:
                print(f"❌ สร้างเสียงไม่ได้ (edge-tts): {type(e).__name__}: {e!r}", flush=True)
                continue

            size = os.path.getsize(mp3) if os.path.exists(mp3) else 0
            print(f"🔎 ไฟล์เสียง {size} bytes | ffmpeg={FFMPEG_EXE}", flush=True)
            if size == 0:
                print("❌ ไฟล์เสียงว่าง (edge-tts ไม่ส่งเสียงกลับมา)", flush=True)
                continue

            # 2) เล่นในห้องเสียง
            while vc.is_playing():
                await asyncio.sleep(0.3)

            done = asyncio.Event()
            play_err = {}
            def _after(err):
                if err:
                    play_err["e"] = err
                bot.loop.call_soon_threadsafe(done.set)

            source = discord.FFmpegPCMAudio(mp3, executable=FFMPEG_EXE)
            vc.play(source, after=_after)
            await done.wait()

            if play_err.get("e"):
                print(f"❌ เล่นเสียงไม่ได้ (playback): {type(play_err['e']).__name__}: {play_err['e']!r}", flush=True)
            else:
                print("🔊 พูดสำเร็จ", flush=True)

        except Exception as e:
            print(f"speak error: {type(e).__name__}: {e!r}", flush=True)
        finally:
            if mp3 and os.path.exists(mp3):
                try:
                    os.remove(mp3)
                except Exception:
                    pass
            speak_queue.task_done()


# ---------------- discord events ----------------
@bot.event
async def on_ready():
    print(f"✅ Discord bot ล็อกอิน: {bot.user}", flush=True)
    print(f"🎧 ใช้ ffmpeg จาก: {FFMPEG_EXE}", flush=True)
    _load_opus()
    if not getattr(bot, "_started", False):
        bot._started = True
        bot.loop.create_task(speak_worker())
        poll_config.start()


# ---------------- telegram ----------------
async def start_telegram():
    await tg_client.start()
    me = await tg_client.get_me()
    print(f"✅ Telegram ล็อกอิน: {me.first_name}", flush=True)

    async for d in tg_client.iter_dialogs():
        for g in TARGET_GROUPS:
            if g in (d.name or "") or str(d.id) == g:
                _tg_targets[d.id] = d.name
                print(f"เจอกลุ่ม: {d.name} (id: {d.id})", flush=True)

    if not _tg_targets:
        print("❌ ไม่เจอกลุ่ม — รายชื่อทั้งหมด:", flush=True)
        async for d in tg_client.iter_dialogs():
            if d.is_group or d.is_channel:
                print(f"  - {d.name} (id: {d.id})", flush=True)
        return

    @tg_client.on(events.NewMessage(chats=list(_tg_targets.keys())))
    async def handler(event):
        if not cfg.get("enabled"):
            return
        raw = (event.raw_text or "").strip()
        if not raw:
            return
        kw = (cfg.get("keyword_filter") or "").strip()
        if kw:
            words = [w.strip() for w in kw.split(",") if w.strip()]
            if words and not any(w in raw for w in words):
                return
        sender = await event.get_sender()
        name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "สมาชิก"
        text = build_announcement(name, raw)
        if text:
            print(f"[อ่าน] {text[:70]}", flush=True)
            await speak_queue.put(text)

    print(f"✅ พร้อมฟัง {len(_tg_targets)} กลุ่ม", flush=True)


async def main():
    await start_telegram()
    await asyncio.gather(
        bot.start(DISCORD_TOKEN),
        tg_client.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
