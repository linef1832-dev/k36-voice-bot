# -*- coding: utf-8 -*-
"""
Telegram → Discord Voice  (เวอร์ชันเต็ม)
ฟีเจอร์:
  - หลายกลุ่ม × 3 กะ, แต่ละห้องมีข้อความเฉพาะ
  - จับคำ → เข้าห้อง → (ติ๊งต่อง) → พูดซ้ำหลายรอบ → ออกจากห้อง
  - ปุ่มทดสอบเสียงจากเว็บ (สั่งพูดทันที)
  - ตั้งเวลาพูดเองตามนาฬิกา (เวลาไทย)
  - เขียนสถานะสด + ประวัติการพูด ให้เว็บอ่าน
  - กันจับซ้ำ (ภายใน N วินาที) + ตั้งช่วงเวลาทำงานของแต่ละกะ

Config เก็บใน Supabase settings (key = tts_voice_config)
บอทเขียน: tts_status (สถานะ), tts_logs (ประวัติ) ; อ่าน: tts_command (ปุ่มทดสอบ)
"""

import asyncio
import ctypes.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta

import discord
import edge_tts
import requests
from discord.ext import tasks
from telethon import TelegramClient, events
from telethon.sessions import StringSession


# ---------- ffmpeg ----------
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
CHIME_PATH = None  # จะสร้างตอน startup


def _make_chime():
    """สร้างไฟล์เสียงเตือน (ติ๊งต่อง) ครั้งเดียว"""
    global CHIME_PATH
    try:
        path = os.path.join(tempfile.gettempdir(), "tts_chime.mp3")
        cmd = [
            FFMPEG_EXE, "-y",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=0.15",
            "-f", "lavfi", "-i", "sine=frequency=1174:duration=0.22",
            "-filter_complex", "[0][1]concat=n=2:v=0:a=1,volume=0.6",
            "-ar", "48000", path,
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            CHIME_PATH = path
            print(f"✅ สร้างเสียงเตือนแล้ว: {path}", flush=True)
    except Exception as e:
        print(f"สร้างเสียงเตือนไม่ได้: {e}", flush=True)


# ---------- opus ----------
def _load_opus():
    if discord.opus.is_loaded():
        return True
    cands = ["libopus.so.0", "libopus.so", "opus",
             "/usr/lib/x86_64-linux-gnu/libopus.so.0", "/usr/lib/libopus.so.0"]
    try:
        f = ctypes.util.find_library("opus")
        if f:
            cands.insert(0, f)
    except Exception:
        pass
    for n in cands:
        try:
            discord.opus.load_opus(n)
            if discord.opus.is_loaded():
                print(f"✅ โหลด libopus จาก: {n}", flush=True)
                return True
        except Exception:
            continue
    print("❌ โหลด libopus ไม่ได้", flush=True)
    return False


# ---------- ENV ----------
TG_API_ID   = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH = os.environ.get("TG_API_HASH", "")
TG_SESSION  = os.environ.get("TG_SESSION", "")
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

_req = {"TG_API_ID": TG_API_ID, "TG_API_HASH": TG_API_HASH, "TG_SESSION": TG_SESSION,
        "DISCORD_TOKEN": DISCORD_TOKEN, "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY}
_missing = [k for k, v in _req.items() if not v]
if _missing:
    print(f"❌ ยังไม่ได้ตั้งค่า: {', '.join(_missing)}", flush=True)
    sys.exit(1)

_SB_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}


# ---------- state ----------
tg_client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)
intents = discord.Intents.default()
intents.voice_states = True
bot = discord.Client(intents=intents)

speak_queue: "asyncio.Queue[dict]" = asyncio.Queue()
cfg = {"speech_rate": "-15%", "chime_enabled": True, "dedupe_seconds": 60, "groups": [], "schedules": []}

_current_room = ""
_last_spoke_at = ""
_last_cmd_id = ""
_dedupe = {}            # (group,shift,kw) -> ts
_sched_fired = {}       # index -> "YYYY-MM-DD HH:MM"


def _bkk_now():
    return datetime.utcnow() + timedelta(hours=7)  # ไทย = UTC+7


def _hm():
    return _bkk_now().strftime("%H:%M")


def _in_window(now_hm, start, end):
    if not start or not end:
        return True
    if start <= end:
        return start <= now_hm <= end
    return now_hm >= start or now_hm <= end  # ข้ามเที่ยงคืน


# ---------- Supabase helpers ----------
def _sb_get_setting(key):
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/settings", headers=_SB_HEADERS,
                         params={"key": f"eq.{key}", "select": "value"}, timeout=10)
        if r.status_code == 200 and r.json():
            raw = r.json()[0].get("value")
            return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        print(f"อ่าน {key} ไม่ได้: {e}", flush=True)
    return None


def _sb_upsert_setting(key, value):
    try:
        requests.post(f"{SUPABASE_URL}/rest/v1/settings",
                      headers={**_SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
                      json={"key": key, "value": json.dumps(value)}, timeout=10)
    except Exception as e:
        print(f"เขียน {key} ไม่ได้: {e}", flush=True)


def _sb_insert_log(row):
    try:
        requests.post(f"{SUPABASE_URL}/rest/v1/tts_logs",
                      headers={**_SB_HEADERS, "Prefer": "return=minimal"},
                      json=row, timeout=10)
    except Exception as e:
        print(f"เขียน log ไม่ได้: {e}", flush=True)


# ---------- rooms helper ----------
def _shift_rooms(sh):
    out = []
    rooms = sh.get("rooms")
    if isinstance(rooms, list) and rooms:
        for r in rooms:
            rid = str(r.get("id") or "").strip()
            txt = (r.get("text") or "").strip() or (sh.get("announce_text") or "").strip()
            if rid and txt:
                out.append((rid, txt))
    else:
        ids = sh.get("voice_channel_ids") or ([sh.get("voice_channel_id")] if sh.get("voice_channel_id") else [])
        txt = (sh.get("announce_text") or "").strip()
        for rid in ids:
            rid = str(rid or "").strip()
            if rid and txt:
                out.append((rid, txt))
    return out


# ---------- loops ----------
@tasks.loop(seconds=5)
async def config_loop():
    global _last_cmd_id
    new = _sb_get_setting("tts_voice_config")
    if isinstance(new, dict):
        cfg.update(new)

    # ปุ่มทดสอบจากเว็บ
    cmd = _sb_get_setting("tts_command")
    if isinstance(cmd, dict) and cmd.get("id") and cmd.get("id") != _last_cmd_id:
        _last_cmd_id = cmd.get("id")
        rid = str(cmd.get("room_id") or "").strip()
        txt = (cmd.get("text") or "").strip()
        if rid and txt:
            print(f"[ทดสอบ] ห้อง={rid}", flush=True)
            await speak_queue.put({
                "room_id": rid, "text": txt,
                "voice_name": cmd.get("voice_name") or "th-TH-PremwadeeNeural",
                "repeat": int(cmd.get("repeat") or 1),
                "log": {"kind": "ทดสอบ", "group": "-", "shift": "-", "keyword": "-"},
            })


@tasks.loop(seconds=10)
async def status_loop():
    online_room = ""
    try:
        if bot.voice_clients and bot.voice_clients[0].is_connected():
            ch = bot.voice_clients[0].channel
            online_room = ch.name if ch else ""
    except Exception:
        pass
    _sb_upsert_setting("tts_status", {
        "online": True,
        "current_room": online_room or _current_room,
        "last_spoke_at": _last_spoke_at,
        "updated_at": _bkk_now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@tasks.loop(seconds=20)
async def schedule_loop():
    now_hm = _hm()
    stamp = _bkk_now().strftime("%Y-%m-%d %H:%M")
    for i, sc in enumerate(cfg.get("schedules") or []):
        if not sc.get("enabled"):
            continue
        if (sc.get("time") or "") != now_hm:
            continue
        if _sched_fired.get(i) == stamp:
            continue
        _sched_fired[i] = stamp
        voice = sc.get("voice_name") or "th-TH-PremwadeeNeural"
        repeat = int(sc.get("repeat") or 1)
        for rid, txt in _shift_rooms(sc):
            print(f"[ตั้งเวลา] {now_hm} '{sc.get('name')}' ห้อง={rid}", flush=True)
            await speak_queue.put({
                "room_id": rid, "text": txt, "voice_name": voice, "repeat": repeat,
                "log": {"kind": "ตั้งเวลา", "group": sc.get("name") or "-", "shift": now_hm, "keyword": "-"},
            })


# ---------- play helpers ----------
async def _play_file(vc, path):
    while vc.is_playing():
        await asyncio.sleep(0.2)
    done = asyncio.Event()
    err = {}
    def _after(e):
        if e:
            err["e"] = e
        bot.loop.call_soon_threadsafe(done.set)
    vc.play(discord.FFmpegPCMAudio(path, executable=FFMPEG_EXE), after=_after)
    await done.wait()
    return not err.get("e")


async def speak_worker():
    global _current_room, _last_spoke_at
    while True:
        job = await speak_queue.get()
        vc = None
        mp3 = None
        try:
            room_id = int(job["room_id"])
            text = (job.get("text") or "").strip()
            voice = job.get("voice_name") or "th-TH-PremwadeeNeural"
            rate = cfg.get("speech_rate") or "-15%"
            repeat = max(1, int(job.get("repeat") or 1))
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
                continue

            existing = bot.voice_clients[0] if bot.voice_clients else None
            if existing and existing.is_connected():
                vc = existing
                if vc.channel.id != room_id:
                    await vc.move_to(ch)
            else:
                vc = await ch.connect(reconnect=True, self_deaf=True)
            _current_room = ch.name
            print(f"✅ เข้าห้อง: {ch.name}", flush=True)

            # เสียงเตือน
            if cfg.get("chime_enabled") and CHIME_PATH:
                try:
                    await _play_file(vc, CHIME_PATH)
                except Exception:
                    pass

            # สร้างเสียงพูด
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                mp3 = f.name
            try:
                await edge_tts.Communicate(text, voice, rate=rate).save(mp3)
            except Exception as e:
                print(f"❌ สร้างเสียงไม่ได้: {e!r}", flush=True)
                continue
            if not os.path.exists(mp3) or os.path.getsize(mp3) == 0:
                continue

            for n in range(repeat):
                ok = await _play_file(vc, mp3)
                print(f"🔊 พูด ({n+1}/{repeat})" + ("" if ok else " (error)"), flush=True)
                if n < repeat - 1:
                    await asyncio.sleep(0.8)

            _last_spoke_at = _bkk_now().strftime("%Y-%m-%d %H:%M:%S")

            # log
            meta = job.get("log")
            if meta:
                _sb_insert_log({
                    "kind": meta.get("kind", "จับคำ"),
                    "group_name": meta.get("group", "-"),
                    "shift_name": meta.get("shift", "-"),
                    "keyword": meta.get("keyword", "-"),
                    "room_name": _current_room,
                    "message": text[:300],
                })
        except Exception as e:
            print(f"speak error: {type(e).__name__}: {e!r}", flush=True)
        finally:
            try:
                if vc and vc.is_connected():
                    await vc.disconnect(force=True)
                    print("👋 ออกจากห้องแล้ว", flush=True)
            except Exception:
                pass
            _current_room = ""
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
    _make_chime()
    if not getattr(bot, "_started", False):
        bot._started = True
        bot.loop.create_task(speak_worker())
        config_loop.start()
        status_loop.start()
        schedule_loop.start()


# ---------- telegram ----------
async def start_telegram():
    await tg_client.start()
    me = await tg_client.get_me()
    print(f"✅ Telegram ล็อกอิน: {me.first_name}", flush=True)

    @tg_client.on(events.NewMessage())
    async def handler(event):
        raw = (event.raw_text or "").strip()
        if not raw or not (event.is_group or event.is_channel):
            return
        try:
            chat = await event.get_chat()
            title = getattr(chat, "title", "") or ""
        except Exception:
            title = ""

        now_hm = _hm()
        dedupe_sec = int(cfg.get("dedupe_seconds") or 0)

        for grp in cfg.get("groups") or []:
            gname = (grp.get("telegram_group") or "").strip()
            if not gname or gname not in title:
                continue
            for sh in grp.get("shifts") or []:
                if not sh.get("enabled"):
                    continue
                kw = (sh.get("keyword") or "").strip()
                if not kw or kw not in raw:
                    continue
                # ช่วงเวลาทำงานของกะ
                if not _in_window(now_hm, (sh.get("active_start") or "").strip(), (sh.get("active_end") or "").strip()):
                    print(f"[ข้ามนอกเวลา] กะ='{sh.get('name')}' ({sh.get('active_start')}-{sh.get('active_end')})", flush=True)
                    continue
                # กันจับซ้ำ
                dk = (gname, sh.get("name"), kw)
                if dedupe_sec > 0 and (time.time() - _dedupe.get(dk, 0)) < dedupe_sec:
                    print(f"[ข้ามซ้ำ] กะ='{sh.get('name')}' คำ='{kw}'", flush=True)
                    continue
                _dedupe[dk] = time.time()

                repeat = max(1, int(sh.get("repeat") or 1))
                voice = sh.get("voice_name") or "th-TH-PremwadeeNeural"
                pairs = _shift_rooms(sh)
                if not pairs:
                    continue
                print(f"[ตรงคำ] กลุ่ม='{title}' กะ='{sh.get('name')}' คำ='{kw}' ห้อง={len(pairs)} รอบ={repeat}", flush=True)
                for rid, txt in pairs:
                    await speak_queue.put({
                        "room_id": rid, "text": txt, "voice_name": voice, "repeat": repeat,
                        "log": {"kind": "จับคำ", "group": title, "shift": sh.get("name"), "keyword": kw},
                    })

    print("✅ พร้อมฟังข้อความแล้ว", flush=True)


async def main():
    await start_telegram()
    await asyncio.gather(
        bot.start(DISCORD_TOKEN),
        tg_client.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
