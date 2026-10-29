# -*- coding: utf-8 -*-
"""
Telegram → Discord Voice  (เวอร์ชันกองบอท Multi-Bot)
ฟีเจอร์เดิมครบ:
  - หลายกลุ่ม × 3 กะ, แต่ละห้องมีข้อความเฉพาะ
  - จับคำ → เข้าห้อง → (ติ๊งต่อง) → พูดซ้ำหลายรอบ → ออกจากห้อง
  - ปุ่มทดสอบเสียงจากเว็บ (สั่งพูดทันที)
  - ตั้งเวลาพูดเองตามนาฬิกา (เวลาไทย)
  - เขียนสถานะสด + ประวัติการพูด ให้เว็บอ่าน
  - กันจับซ้ำ (ภายใน N วินาที) + ตั้งช่วงเวลาทำงานของแต่ละกะ

🤖 ใหม่: กองบอทหลายตัวพูดขนานกัน
  - บอทหลัก = DISCORD_TOKEN ใน ENV (เหมือนเดิม ไม่ต้องแตะ)
  - บอทเสริม = เพิ่มจากหน้าเว็บ (แท็บ "กองบอท") เก็บใน tts_voice_config.bots
  - แต่ละตัวมีคิวของตัวเอง → 9 ห้อง + 3 บอท = เสร็จใน ~3 รอบพูด แทนที่จะเป็น 9
  - โหมดแบ่งงาน: auto (กระจายให้ตัวที่ว่างสุด) / manual (ห้องผูกบอทได้, ที่เหลือ auto)
  - เพิ่ม/ลบ/ปิดบอทจากเว็บ มีผลใน ~5 วินาที ไม่ต้อง restart

Config เก็บใน Supabase settings (key = tts_voice_config)
บอทเขียน: tts_status (สถานะรายตัว), tts_logs (ประวัติ+ชื่อบอท) ; อ่าน: tts_command (ปุ่มทดสอบ, มี bot_id ได้)
"""

import asyncio
import contextlib
import ctypes.util
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone

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


# ---------- DAVE (Discord E2EE voice) ----------
# ตั้งแต่ มี.ค. 2026 Discord บังคับเข้ารหัสเสียง (DAVE) ทุกห้อง — เสียงที่ส่งไปแบบไม่เข้ารหัสจะเงียบ
# ต้องใช้ discord.py >= 2.7 + แพ็กเกจ davey และต้อง "รอให้ DAVE พร้อม" ก่อนเริ่มเล่นเสียง
def _check_dave():
    try:
        import davey  # noqa: F401
        print(f"✅ discord.py {discord.__version__} + davey (DAVE E2EE) พร้อม", flush=True)
    except Exception:
        print(f"❌ discord.py {discord.__version__} ไม่มี davey — บอทจะเข้าห้องได้แต่เสียงจะเงียบ! "
              f"แก้ requirements.txt เป็น discord.py[voice]>=2.7.1 แล้ว redeploy", flush=True)


async def _wait_dave_ready(vc, timeout=1.0):
    """รอให้ voice client ต่อเสร็จ + DAVE session พร้อมเข้ารหัส ก่อนเล่นเสียง (กันพูดแล้วเงียบ)"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        conn = getattr(vc, "_connection", None)
        can = getattr(conn, "can_encrypt", None)
        ver = getattr(conn, "dave_protocol_version", None)
        if vc.is_connected():
            if can is None:               # discord.py เก่า ไม่มี DAVE
                return True
            if can:                       # DAVE พร้อมแล้ว
                return True
            if ver == 0 and conn.dave_session is None and time.monotonic() - t0 > 2.0:
                return True               # ห้องนี้ไม่ใช้ E2EE (เช่น Stage) — ไปต่อได้
        await asyncio.sleep(0.1)
    return False


def _make_chime():
    """สร้างไฟล์เสียงเตือน (ติ๊งต่อง) ครั้งเดียว"""
    global CHIME_PATH
    if CHIME_PATH:
        return
    try:
        path = os.path.join(tempfile.gettempdir(), "tts_chime.ogg")
        cmd = [
            FFMPEG_EXE, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=0.15",
            "-f", "lavfi", "-i", "sine=frequency=1174:duration=0.22",
            "-filter_complex", "[0][1]concat=n=2:v=0:a=1,volume=0.6",
            "-ar", "48000", "-ac", "2", "-c:a", "libopus", "-b:a", "64k", "-application", "voip", path,
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

cfg = {"speech_rate": "-15%", "chime_enabled": True, "dedupe_seconds": 60,
       "groups": [], "schedules": [], "bots": [], "dispatch_mode": "auto"}

_last_cmd_id = ""
_seen_msgs = {}         # (chat_id, message_id) -> {ts, spoke, text}  กันพูดซ้ำจากข้อความชิ้นเดียวกัน
_dedupe = {}            # (group,shift,kw,ข้อความ) -> ts  (นับเฉพาะข้อความ "เดิมเป๊ะ" ว่าซ้ำ
                        #  ประกาศ รอบที่ 1 / รอบที่ 2 = คนละข้อความ จะไม่โดนข้าม)
_sched_fired = {}       # index -> "YYYY-MM-DD HH:MM"
_logs_has_bot_col = True   # ถ้าตาราง tts_logs ยังไม่มีคอลัมน์ bot_name จะ fallback อัตโนมัติ


def _bkk_now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=7)  # ไทย = UTC+7


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
    """เขียน log — ถ้าตารางยังไม่มีคอลัมน์ bot_name (ยังไม่ได้รัน ALTER TABLE) ให้ตัดออกแล้วส่งใหม่"""
    global _logs_has_bot_col
    try:
        if not _logs_has_bot_col:
            row = {k: v for k, v in row.items() if k != "bot_name"}
        r = requests.post(f"{SUPABASE_URL}/rest/v1/tts_logs",
                          headers={**_SB_HEADERS, "Prefer": "return=minimal"},
                          json=row, timeout=10)
        if r.status_code >= 400 and "bot_name" in row:
            _logs_has_bot_col = False
            print("⚠️ tts_logs ยังไม่มีคอลัมน์ bot_name (รัน: ALTER TABLE tts_logs ADD COLUMN bot_name text;) — ส่ง log แบบไม่มีชื่อบอทไปก่อน", flush=True)
            row.pop("bot_name", None)
            requests.post(f"{SUPABASE_URL}/rest/v1/tts_logs",
                          headers={**_SB_HEADERS, "Prefer": "return=minimal"},
                          json=row, timeout=10)
    except Exception as e:
        print(f"เขียน log ไม่ได้: {e}", flush=True)


# ---------- rooms helper ----------
def _shift_rooms(sh):
    """คืน [(room_id, text, bot_id)] — bot_id = บอทที่ห้องนี้ผูกไว้ (โหมด manual), '' = ให้แบ่งอัตโนมัติ"""
    out = []
    rooms = sh.get("rooms")
    if isinstance(rooms, list) and rooms:
        for r in rooms:
            rid = str(r.get("id") or "").strip()
            txt = (r.get("text") or "").strip() or (sh.get("announce_text") or "").strip()
            bid = str(r.get("bot_id") or "").strip()
            if rid and txt:
                out.append((rid, txt, bid))
    else:
        ids = sh.get("voice_channel_ids") or ([sh.get("voice_channel_id")] if sh.get("voice_channel_id") else [])
        txt = (sh.get("announce_text") or "").strip()
        for rid in ids:
            rid = str(rid or "").strip()
            if rid and txt:
                out.append((rid, txt, ""))
    return out


# ══════════════════════════════════════════════════════════════════
# 🤖 กองบอท (Fleet) — บอทหลายตัว แต่ละตัวมีคิว+worker ของตัวเอง
# ══════════════════════════════════════════════════════════════════
class BotUnit:
    def __init__(self, bot_id, name, token, is_main=False):
        self.id = str(bot_id)
        self.name = name or self.id
        self.token = token
        self.is_main = is_main
        intents = discord.Intents.default()
        intents.voice_states = True
        self.client = discord.Client(intents=intents)
        self.queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self.current_room = ""
        self.last_spoke_at = ""
        self._client_task = None
        self._worker_task = None
        self._stopping = False

        unit = self

        @self.client.event
        async def on_ready():
            print(f"✅ [{unit.display_name()}] Discord ล็อกอิน: {unit.client.user}", flush=True)
            _load_opus()
            _make_chime()
            _check_dave()

    def display_name(self):
        if self.is_main and self.client.user:
            return self.name or str(self.client.user)
        return self.name

    def is_ready(self):
        try:
            return self.client.is_ready() and not self.client.is_closed()
        except Exception:
            return False

    def live_room(self):
        """ห้องที่กำลังอยู่จริงตอนนี้ (จาก voice client) หรือห้องที่ worker จำไว้"""
        try:
            if self.client.voice_clients and self.client.voice_clients[0].is_connected():
                ch = self.client.voice_clients[0].channel
                if ch:
                    return ch.name
        except Exception:
            pass
        return self.current_room

    def busy_score(self):
        """คะแนนความยุ่ง — ใช้เลือกบอทที่ว่างสุดตอนแบ่งงาน"""
        s = self.queue.qsize()
        try:
            if self.client.voice_clients:
                s += 1  # กำลังอยู่ในห้อง = ยุ่งอยู่
        except Exception:
            pass
        return s

    def start(self):
        self._client_task = asyncio.create_task(self._run_client(), name=f"client:{self.id}")
        self._worker_task = asyncio.create_task(speak_worker(self), name=f"worker:{self.id}")

    async def _run_client(self):
        try:
            await self.client.start(self.token)
        except discord.LoginFailure:
            print(f"❌ [{self.name}] Token ไม่ถูกต้อง — เช็ค Token ในหน้าเว็บ (แท็บกองบอท)", flush=True)
        except Exception as e:
            if not self._stopping:
                print(f"❌ [{self.name}] client หลุด: {type(e).__name__}: {e!r}", flush=True)

    async def stop(self):
        self._stopping = True
        for t in (self._worker_task,):
            if t:
                t.cancel()
        try:
            await self.client.close()
        except Exception:
            pass
        if self._client_task:
            try:
                await asyncio.wait_for(self._client_task, timeout=10)
            except Exception:
                pass
        print(f"🛑 [{self.name}] ปิดแล้ว", flush=True)


FLEET: "dict[str, BotUnit]" = {}   # bot_id -> BotUnit ("main" = บอทจาก ENV)


def _fleet_sig(bots_cfg):
    """ลายเซ็น config บอทเสริม — เปลี่ยนเมื่อ เพิ่ม/ลบ/แก้ token/เปิดปิด"""
    return json.dumps([[str(b.get("id")), (b.get("token") or "").strip(), bool(b.get("enabled", True))]
                       for b in (bots_cfg or [])], sort_keys=True)

_last_fleet_sig = None


async def sync_fleet():
    """เทียบ config กับกองบอทปัจจุบัน → เปิดตัวใหม่ / ปิดตัวที่ถูกลบ / อัปเดตชื่อ"""
    global _last_fleet_sig
    bots_cfg = cfg.get("bots") or []
    sig = _fleet_sig(bots_cfg)
    if sig == _last_fleet_sig:
        # config ไม่เปลี่ยน — อัปเดตแค่ชื่อ (ชื่อเปลี่ยนไม่ต้อง restart client)
        for b in bots_cfg:
            u = FLEET.get(str(b.get("id")))
            if u and b.get("name"):
                u.name = b["name"]
        return
    _last_fleet_sig = sig

    want = {}
    for b in bots_cfg:
        bid = str(b.get("id") or "").strip()
        tok = (b.get("token") or "").strip()
        if bid and tok and b.get("enabled", True):
            want[bid] = b

    # ปิดบอทที่ถูกลบ/ปิดใช้/เปลี่ยน token
    for bid in list(FLEET.keys()):
        if bid == "main":
            continue
        u = FLEET[bid]
        wb = want.get(bid)
        if not wb or (wb.get("token") or "").strip() != u.token:
            del FLEET[bid]
            asyncio.create_task(u.stop())

    # เปิดบอทตัวใหม่
    for bid, b in want.items():
        if bid in FLEET:
            FLEET[bid].name = b.get("name") or FLEET[bid].name
            continue
        u = BotUnit(bid, b.get("name") or f"บอท {bid[-4:]}", (b.get("token") or "").strip())
        FLEET[bid] = u
        u.start()
        print(f"🚀 สตาร์ทบอทเสริม: {u.name}", flush=True)


def _online_units():
    return [u for u in FLEET.values() if u.is_ready()]


async def dispatch_jobs(jobs):
    """แบ่งงานให้กองบอท — หัวใจของความเร็ว
    manual + ห้องผูกบอท → เข้าคิวบอทตัวนั้น (ถ้าออนไลน์)
    ที่เหลือ → บอทออนไลน์ที่ว่างสุด (นับงานที่เพิ่งแจกรอบนี้ด้วย ให้กระจายจริง)"""
    units = _online_units()
    if not units:
        units = [FLEET["main"]]  # ยังไม่มีตัวไหน ready → เข้าคิวบอทหลักไว้ก่อน เดี๋ยว ready แล้วพูดเอง
    manual = (cfg.get("dispatch_mode") == "manual")
    pending = {u.id: 0 for u in units}   # งานที่แจกไปแล้วในรอบนี้

    for job in jobs:
        target = None
        bid = str(job.get("bot_id") or "")
        if manual and bid:
            u = FLEET.get(bid)
            if u and u.is_ready():
                target = u
                if bid not in pending:
                    pending[bid] = 0
        if target is None:
            target = min(units, key=lambda u: u.busy_score() + pending.get(u.id, 0))
        pending[target.id] = pending.get(target.id, 0) + 1
        await target.queue.put(job)

    if len(jobs) > 1:
        used = [FLEET[i].display_name() for i, n in pending.items() if n > 0]
        print(f"📦 แจก {len(jobs)} ห้อง → {len(used)} บอท: {', '.join(used)}", flush=True)


# ---------- loops (ผูกกับบอทหลัก) ----------
@tasks.loop(seconds=5)
async def config_loop():
    global _last_cmd_id
    new = _sb_get_setting("tts_voice_config")
    if isinstance(new, dict):
        cfg.update(new)
    await sync_fleet()

    # ปุ่มทดสอบจากเว็บ (มี bot_id = ให้บอทตัวนั้นพูดโดยเฉพาะ)
    cmd = _sb_get_setting("tts_command")
    if isinstance(cmd, dict) and cmd.get("id") and cmd.get("id") != _last_cmd_id:
        _last_cmd_id = cmd.get("id")
        rid = str(cmd.get("room_id") or "").strip()
        txt = (cmd.get("text") or "").strip()
        if rid and txt:
            print(f"[ทดสอบ] ห้อง={rid} บอท={cmd.get('bot_id') or 'auto'}", flush=True)
            # ปุ่มทดสอบไม่มีข้อความจริงจากกลุ่ม จึงไม่มีเลขให้เติม — ใส่เลขตัวอย่างให้ฟังรู้เรื่อง
            txt = _fill_placeholders(txt, "", "", ["100", "10,000", "1,000", "500", "50,000"])
            await dispatch_jobs([{
                "room_id": rid, "text": txt,
                "voice_name": cmd.get("voice_name") or "th-TH-PremwadeeNeural",
                "repeat": int(cmd.get("repeat") or 1),
                "bot_id": str(cmd.get("bot_id") or ""),
                "log": {"kind": "ทดสอบ", "group": "-", "shift": "-", "keyword": "-"},
            }])


@tasks.loop(seconds=10)
async def status_loop():
    now = _bkk_now().strftime("%Y-%m-%d %H:%M:%S")
    bots_arr = []
    for u in FLEET.values():
        bots_arr.append({
            "bot_id": u.id,
            "name": u.display_name(),
            "online": u.is_ready(),
            "current_room": u.live_room() if u.is_ready() else "",
            "last_spoke_at": u.last_spoke_at,
            "updated_at": now,
        })
    main_u = FLEET.get("main")
    _sb_upsert_setting("tts_status", {
        "bots": bots_arr,
        "updated_at": now,
        # ── ฟิลด์รูปเก่า เผื่อหน้าเว็บเวอร์ชันก่อนยังเปิดค้างอยู่ ──
        "online": bool(main_u and main_u.is_ready()),
        "current_room": (main_u.live_room() if main_u else ""),
        "last_spoke_at": (main_u.last_spoke_at if main_u else ""),
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
        jobs = []
        for rid, txt, bid in _shift_rooms(sc):
            print(f"[ตั้งเวลา] {now_hm} '{sc.get('name')}' ห้อง={rid}", flush=True)
            jobs.append({
                "room_id": rid, "text": txt, "voice_name": voice, "repeat": repeat, "bot_id": bid,
                "log": {"kind": "ตั้งเวลา", "group": sc.get("name") or "-", "shift": now_hm, "keyword": "-"},
            })
        if jobs:
            await dispatch_jobs(jobs)


# ต่อห้องเสียงพร้อมกันกี่ตัว: 0 = ไม่จำกัด (ทุกตัวพุ่งเข้าห้องพร้อมกัน) ← ค่าเริ่มต้น
# ตั้ง VOICE_CONCURRENCY=3 ใน Railway ถ้าอยากจำกัด (ช้าลงแต่เสี่ยง TimeoutError น้อยลง)
_VC_LIMIT = int(os.getenv("VOICE_CONCURRENCY", "0") or 0)
_VOICE_GATE = asyncio.Semaphore(_VC_LIMIT) if _VC_LIMIT > 0 else contextlib.nullcontext()


@contextlib.asynccontextmanager
async def _vc_gate():
    if _VC_LIMIT > 0:
        async with _VOICE_GATE:
            yield
    else:
        yield


async def _wait_connected(vc, timeout=10.0):
    """หลัง connect() คืนค่ามาแล้ว ยังต้องรอ websocket เสียงเปิดจริง ไม่งั้นจะเจอ 'Not connected to voice'"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if vc.is_connected():
            return True
        await asyncio.sleep(0.2)
    return False


async def _connect_retry(unit, ch, tries=3):
    last = None
    for i in range(tries):
        try:
            async with _vc_gate():
                vc = await ch.connect(reconnect=True, self_deaf=True, timeout=30)
                if await _wait_connected(vc):
                    return vc
                last = discord.ClientException("connected แต่ websocket เสียงไม่เปิด")
                print(f"⚠️ [{unit.display_name()}] ต่อห้อง {ch.name} แล้วแต่ยังไม่พร้อม (ครั้งที่ {i+1}/{tries}) — ลองใหม่", flush=True)
        except (asyncio.TimeoutError, TimeoutError, discord.ClientException, discord.ConnectionClosed) as e:
            last = e
            print(f"⚠️ [{unit.display_name()}] ต่อห้อง {ch.name} ไม่ติด (ครั้งที่ {i+1}/{tries}): {type(e).__name__} — ลองใหม่", flush=True)
        # เคลียร์ voice client ค้าง แล้วรอ (ถอยนานขึ้นเรื่อยๆ) ก่อนต่อใหม่
        for old in list(unit.client.voice_clients):
            try:
                await old.disconnect(force=True)
            except Exception:
                pass
        await asyncio.sleep(2 + i * 3)
    raise last


_tts_cache = {}         # (text,voice,rate) -> (path, ts)
_tts_locks = {}


def _clean_for_tts(t: str) -> str:
    """เอา {…} ที่ยังเติมไม่ได้ออกก่อนส่งให้ edge-tts
    (ถ้าปล่อยไว้ บริการจะตอบกลับมาเป็นเสียงเปล่า → NoAudioReceived)"""
    t = re.sub(r"\{[^}]{0,20}\}", " ", t or "")
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


async def _tts_cached(text, voice, rate, unit):
    """ประกาศเดียวกันถูกส่งไปหลายห้องพร้อมกัน — สร้างเสียงครั้งเดียวแล้วใช้ร่วมกัน
    (เดิมบอท 7 ตัวต่อสายไป edge-tts พร้อมกัน 8 ครั้ง ทำให้รอนานและเสี่ยงโดนจำกัดอัตรา)"""
    text = _clean_for_tts(text)
    if not text:
        print(f"⛔ [{unit.display_name()}] ข้อความว่างหลังเติมค่า — ไม่พูด", flush=True)
        return None
    key = (text, voice, rate)
    now = time.time()
    for k, (pth, ts) in list(_tts_cache.items()):
        if now - ts > 600:
            _tts_cache.pop(k, None)
            _opus_packets.pop(pth, None)
            for junk in (pth, pth[:-4] + ".mp3"):
                try:
                    os.remove(junk)
                except Exception:
                    pass
    hit = _tts_cache.get(key)
    if hit and os.path.exists(hit[0]):
        return hit[0]

    lock = _tts_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _tts_cache.get(key)
        if hit and os.path.exists(hit[0]):
            return hit[0]
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            mp3 = f.name
        last_err = None
        for attempt in range(5):          # บริการของ Microsoft เด้งเป็นช่วงๆ (403 / NoAudioReceived) — ลองใหม่หลายรอบ
            try:
                await asyncio.wait_for(edge_tts.Communicate(text, voice, rate=rate).save(mp3), timeout=25)
                if os.path.exists(mp3) and os.path.getsize(mp3) > 0:
                    last_err = None
                    break
                last_err = RuntimeError("ไฟล์เสียงว่าง")
            except Exception as e:
                last_err = e
            if attempt < 4:
                await asyncio.sleep(1.2 * (attempt + 1) + random.uniform(0, 0.6))
        if last_err is not None:
            msg = str(last_err)
            if "403" in msg:
                print(f"❌ [{unit.display_name()}] สร้างเสียงไม่ได้: Microsoft ปฏิเสธ (403) — "
                      f"ต้องอัปเกรด edge-tts เป็น 7.3.1 ขึ้นไป แล้ว redeploy", flush=True)
            else:
                print(f"❌ [{unit.display_name()}] สร้างเสียงไม่ได้: {type(last_err).__name__}: {msg[:120]} "
                      f"(ข้อความยาว {len(text)} ตัวอักษร)", flush=True)
            return None
        if not os.path.exists(mp3) or os.path.getsize(mp3) == 0:
            return None
        path = await _mp3_to_opus(mp3) or mp3
        _tts_cache[key] = (path, time.time())
        return path


# ---------- play helpers ----------
async def _mp3_to_opus(mp3):
    """แปลง mp3 จาก edge-tts → Opus 48kHz ครั้งเดียวก่อนเล่น
    (เดิมเล่นตรงจาก mp3 = ffmpeg ถอดรหัส + Python เข้ารหัส Opus แบบเรียลไทม์ทุก 20ms → CPU หนัก → เสียงสะดุด
     แบบใหม่ตอนเล่นแค่ copy แพ็กเก็ตที่เข้ารหัสไว้แล้ว เบากว่ามาก)"""
    ogg = mp3[:-4] + ".ogg"
    cmd = [FFMPEG_EXE, "-y", "-loglevel", "error", "-i", mp3,
           "-ar", "48000", "-ac", "2", "-c:a", "libopus", "-b:a", "64k",
           "-application", "voip", "-frame_duration", "20", ogg]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if os.path.exists(ogg) and os.path.getsize(ogg) > 0:
        return ogg
    return None


_opus_packets = {}      # path -> [แพ็กเก็ต Opus ที่แกะไว้แล้ว]


class MemoryOpusAudio(discord.AudioSource):
    """เล่นเสียงจากแพ็กเก็ต Opus ที่อยู่ใน RAM ตรงๆ
    ไม่ต้องเปิด ffmpeg ตอนเล่น (เดิมบอท 7 ตัว = 7 โปรเซส ffmpeg พร้อมกัน
    ทำให้ CPU แย่งกันแล้วส่งเสียงไม่ทันทุก 20 ms → ได้ยินเป็นกระตุก)"""

    def __init__(self, packets):
        self._packets = packets
        self._i = 0

    def is_opus(self):
        return True

    def read(self):
        if self._i >= len(self._packets):
            return b""
        p = self._packets[self._i]
        self._i += 1
        return p

    def cleanup(self):
        self._packets = []


def _load_opus_packets(path):
    """แกะไฟล์ .ogg เป็นแพ็กเก็ตครั้งเดียว แล้วใช้ซ้ำได้ทุกบอททุกห้อง"""
    cached = _opus_packets.get(path)
    if cached is not None:
        return cached
    try:
        from discord.oggparse import OggStream
        with open(path, "rb") as f:
            packets = list(OggStream(f).iter_packets())
        if packets:
            _opus_packets[path] = packets
            return packets
    except Exception as e:
        print(f"⚠️ แกะไฟล์เสียงไม่ได้ จะใช้ ffmpeg แทน: {type(e).__name__}", flush=True)
    return None


async def _play_file(vc, path):
    while vc.is_playing():
        await asyncio.sleep(0.2)
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    err = {}
    def _after(e):
        if e:
            err["e"] = e
        loop.call_soon_threadsafe(done.set)
    src = None
    if path.endswith(".ogg"):
        packets = _load_opus_packets(path)
        if packets:
            src = MemoryOpusAudio(packets)
        else:
            src = discord.FFmpegOpusAudio(path, codec="copy", executable=FFMPEG_EXE)
    else:
        src = discord.FFmpegPCMAudio(path, executable=FFMPEG_EXE)
    vc.play(src, after=_after)
    await done.wait()
    return not err.get("e")


async def speak_worker(unit: BotUnit):
    """worker ประจำบอท 1 ตัว — ไล่คิวของตัวเอง (หลายบอท = หลาย worker วิ่งพร้อมกัน)"""
    await unit.client.wait_until_ready()
    while True:
        job = await unit.queue.get()
        vc = None
        try:
            if not unit.is_ready():
                await unit.client.wait_until_ready()
            room_id = int(job["room_id"])
            text = (job.get("text") or "").strip()
            voice = job.get("voice_name") or "th-TH-PremwadeeNeural"
            rate = cfg.get("speech_rate") or "-15%"
            repeat = max(1, int(job.get("repeat") or 1))
            if not text:
                continue

            # สร้างเสียงพูด "ก่อน" เข้าห้อง และใช้ไฟล์ร่วมกันทุกห้องที่ข้อความเหมือนกัน
            play_path = await _tts_cached(text, voice, rate, unit)
            if not play_path:
                await asyncio.sleep(2)
                play_path = await _tts_cached(text, voice, rate, unit)   # ลองอีกหน (บอทตัวอื่นอาจสร้างสำเร็จแล้ว)
            if not play_path:
                print(f"⛔ [{unit.display_name()}] ข้ามห้องนี้ เพราะสร้างเสียงไม่สำเร็จ", flush=True)
                continue

            ch = unit.client.get_channel(room_id)
            if ch is None:
                try:
                    ch = await unit.client.fetch_channel(room_id)
                except Exception as e:
                    print(f"[{unit.display_name()}] หาห้องไม่เจอ {room_id}: {e} (บอทตัวนี้ถูกเชิญเข้าเซิร์ฟเวอร์หรือยัง?)", flush=True)
                    continue
            if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                continue

            existing = unit.client.voice_clients[0] if unit.client.voice_clients else None
            if existing and existing.is_connected():
                vc = existing
                if vc.channel.id != room_id:
                    if vc.channel.guild.id == ch.guild.id:
                        try:
                            await vc.move_to(ch)      # เซิร์ฟเวอร์เดียวกัน → ย้ายห้องเลย (เร็ว)
                            if not await _wait_connected(vc, 8):
                                raise discord.ClientException("ย้ายห้องแล้วเสียงไม่ต่อ")
                        except Exception:
                            try:
                                await vc.disconnect(force=True)
                            except Exception:
                                pass
                            vc = await _connect_retry(unit, ch)
                    else:
                        await vc.disconnect(force=True)  # คนละเซิร์ฟเวอร์ → ต้องออกก่อนค่อยเข้าใหม่
                        await asyncio.sleep(1)
                        vc = await _connect_retry(unit, ch)
            else:
                vc = await _connect_retry(unit, ch)
            unit.current_room = ch.name
            print(f"✅ [{unit.display_name()}] เข้าห้อง: {ch.name}", flush=True)

            # 🔐 รอให้ DAVE (E2EE) พร้อมก่อนพูด — ถ้าไม่รอ เสียงชุดแรกจะถูกส่งแบบไม่เข้ารหัสแล้วเงียบ
            await _wait_dave_ready(vc)

            # เช็คอีกรอบก่อนพูด — ถ้าหลุดระหว่างนี้ให้ต่อใหม่ (กัน 'Not connected to voice')
            if not vc.is_connected():
                print(f"⚠️ [{unit.display_name()}] เสียงหลุดก่อนพูด — ต่อห้องใหม่", flush=True)
                vc = await _connect_retry(unit, ch)
                await _wait_dave_ready(vc)

            # เสียงเตือน
            if cfg.get("chime_enabled") and CHIME_PATH:
                try:
                    await _play_file(vc, CHIME_PATH)
                except Exception:
                    pass

            for n in range(repeat):
                ok = await _play_file(vc, play_path)
                print(f"🔊 [{unit.display_name()}] พูด ({n+1}/{repeat})" + ("" if ok else " (error)"), flush=True)
                if n < repeat - 1:
                    await asyncio.sleep(0.8)

            unit.last_spoke_at = _bkk_now().strftime("%Y-%m-%d %H:%M:%S")

            # log
            meta = job.get("log")
            if meta:
                _sb_insert_log({
                    "kind": meta.get("kind", "จับคำ"),
                    "group_name": meta.get("group", "-"),
                    "shift_name": meta.get("shift", "-"),
                    "keyword": meta.get("keyword", "-"),
                    "room_name": unit.current_room,
                    "message": text[:300],
                    "bot_name": unit.display_name(),
                })
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{unit.display_name()}] speak error: {type(e).__name__}: {e!r}", flush=True)
        finally:
            try:
                # 🌟 ถ้าในคิวตัวเองยังมีงานต่อ ไม่ต้องออกจากห้อง — เดี๋ยว move_to ห้องถัดไปเลย (เร็วกว่า)
                if vc and vc.is_connected() and unit.queue.empty():
                    await vc.disconnect(force=True)
                    print(f"👋 [{unit.display_name()}] ออกจากห้องแล้ว", flush=True)
                    unit.current_room = ""
            except Exception:
                unit.current_room = ""
            pass   # ไฟล์เสียงใช้ร่วมกันหลายห้อง ปล่อยให้ _tts_cached เก็บกวาดเอง
            unit.queue.task_done()


# ---------- telegram ----------
async def _publish_telegram_list():
    """ดึงรายชื่อกลุ่ม Telegram ที่บัญชีอยู่ (id + ชื่อ) เก็บใน Supabase ให้เว็บเลือก"""
    try:
        items = []
        async for d in tg_client.iter_dialogs():
            if d.is_group or d.is_channel:
                items.append({"id": str(d.id), "title": d.name or ""})
        _sb_upsert_setting("telegram_groups_list", items)
        print(f"📋 อัปเดตรายชื่อกลุ่ม Telegram: {len(items)} กลุ่ม", flush=True)
    except Exception as e:
        print(f"ดึงรายชื่อกลุ่มไม่ได้: {e}", flush=True)


@tasks.loop(minutes=5)
async def telegram_list_loop():
    await _publish_telegram_list()


_last_tg_update = 0.0          # เวลาล่าสุดที่ "มีข้อความจาก Telegram เข้ามาจริง"
_tg_raw_count = 0              # นับสัญญาณดิบทุกชนิดที่ Telegram ส่งมา
_tg_msg_count = 0              # นับเฉพาะข้อความในกลุ่มที่บอทอ่านจริง


@tasks.loop(minutes=5)
async def telegram_pulse():
    """พิมพ์ชีพจรฝั่ง Telegram ไว้ดูว่า 'ไม่มีข้อความ' เพราะไม่มีใครพิมพ์ หรือเพราะสายขาด"""
    idle = int((time.time() - _last_tg_update) / 60) if _last_tg_update else -1
    print(f"💓 Telegram: สัญญาณดิบ {_tg_raw_count} ครั้ง | ข้อความกลุ่ม {_tg_msg_count} ครั้ง | "
          f"เงียบมา {idle} นาที | ต่ออยู่={tg_client.is_connected()}", flush=True)


@tasks.loop(minutes=2)
async def telegram_watchdog():
    """บางครั้ง Telethon ยังต่ออยู่ (ดึงรายชื่อกลุ่มได้) แต่สายรับข้อความตายเงียบๆ
    ผลคือบอทไม่รู้เลยว่ามีประกาศเข้ามา — ถ้าเงียบนานผิดปกติให้ต่อใหม่"""
    global _last_tg_update
    if not _last_tg_update:
        _last_tg_update = time.time()
        return
    idle = time.time() - _last_tg_update
    if idle < 900:             # 15 นาที
        return
    print(f"⚠️ ไม่ได้รับข้อความ Telegram มา {int(idle/60)} นาที — ต่อใหม่", flush=True)
    try:
        await tg_client.disconnect()
    except Exception:
        pass
    try:
        await tg_client.connect()
        if not await tg_client.is_user_authorized():
            print("❌ ต่อ Telegram ใหม่แล้วแต่ session หมดอายุ — ต้องสร้าง TG_SESSION ใหม่", flush=True)
        else:
            try:
                await tg_client.catch_up()
            except Exception:
                pass
            _last_tg_update = time.time()
            print("✅ ต่อ Telegram ใหม่เรียบร้อย", flush=True)
    except Exception as e:
        print(f"❌ ต่อ Telegram ใหม่ไม่สำเร็จ: {type(e).__name__}: {e}", flush=True)


_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\ufeff\u00ad\ufe0e\ufe0f"), None)


def _norm_text(t: str) -> str:
    """ทำข้อความให้เทียบกันได้ — บอท Telegram มักพิมพ์ช่องว่างไม่เหมือนคน (NBSP, zero-width,
    วงเล็บเต็มความกว้าง, อีโมจิคั่น) ทำให้ 'คำที่ตั้งไว้' หาไม่เจอทั้งที่ตาเรามองว่าเหมือนกัน"""
    t = unicodedata.normalize("NFKC", t or "")
    t = t.translate(_ZW)
    t = t.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ")
    # ทำให้ตัวคั่นเวลาเป็นแบบเดียวกันหมด: 08.00 / 08:00 / 08：00 / 08-00 → 08:00
    t = re.sub(r"(?<=\d)\s*[.:：ː∶﹕·•]\s*(?=\d)", ":", t)
    # ขีดคั่นช่วงเวลาแบบต่างๆ (– — ~ ถึง) → -
    t = re.sub(r"\s*[-–—−~〜]\s*", "-", t)
    # ช่องว่างติดวงเล็บ: "กะเช้า (08:00" กับ "กะเช้า(08:00" ให้ถือว่าเหมือนกัน
    t = re.sub(r"\s*([()\[\]{}])\s*", r"\1", t)
    # เครื่องหมายถูก/ผิด มีหลายแบบมาก — ยุบให้เหลือแบบเดียว จะได้ไม่พลาดเวลาเขาเปลี่ยนอีโมจิ
    t = re.sub("[\u2705\u2714\u2611\u2713\U0001F7E2]", "\u2705", t)
    t = re.sub("[\u274C\u2716\u274E\u2717\u2718\U0001F534\U0001F6AB]", "\u274C", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()


_NUM_RE = re.compile(r"\d[\d,\.]*")


def _kw_match(kw: str, raw: str, raw_n: str):
    """เทียบคำที่ตั้งไว้กับข้อความ — รองรับ * แทน "ตัวเลขอะไรก็ได้"
    เช่นตั้งว่า  ระบบถอนปกติ ขั้นต่ำ * - *
    จะจับได้ทุกแบบ: 300 - 20,000 / 100 - 100,000 / 500 - 10,000 …
    และตัวเลขที่จับได้จะกลายเป็น {1} {2} ให้ใช้ในข้อความพูดทันที

    การเว้นวรรคไม่มีผล — จะเว้นเกิน เว้นขาด หรือไม่เว้นเลยก็จับได้เหมือนกัน
    คืนค่า (ตรงไหม, [ตัวเลขที่จับได้])"""
    nospace = lambda t: re.sub(r"\s+", "", t)
    if "*" in kw:
        parts = [re.escape(nospace(p)) for p in _norm_text(kw).split("*")]
        pattern = r"(\d[\d,\.]*)".join(parts)
        m = re.search(pattern, nospace(raw_n))
        if m:
            return True, list(m.groups())
        return False, []
    if kw in raw or _norm_text(kw) in raw_n or nospace(_norm_text(kw)) in nospace(raw_n):
        return True, _NUM_RE.findall(kw)
    return False, []


def _fill_placeholders(text: str, kw: str, msg: str, kw_nums=None) -> str:
    """เติมช่องว่างในข้อความอัตโนมัติ — จะได้ไม่ต้องพิมพ์ข้อความทีละคำ
       {1} {2} {3} … = ตัวเลขตัวที่ 1,2,3 ในคำที่จับได้
       {ค1} {ค2} …   = ตัวเลขในข้อความจริงที่ส่งเข้ามาในกลุ่ม
       {คำ}          = คำที่จับได้ทั้งคำ
    ตัวอย่าง: คำ='ระบบถอนปกติ ขั้นต่ำ 500 - 10,000'
              ข้อความ='ถอนเงินได้ {1} ถึง {2} บาท'  →  'ถอนเงินได้ 500 ถึง 10,000 บาท'"""
    if not text or "{" not in text:
        return text
    kw_nums = list(kw_nums) if kw_nums is not None else _NUM_RE.findall(kw or "")
    msg_nums = _NUM_RE.findall(msg or "")

    def pick(m):
        tok = m.group(1).strip()
        if tok in ("คำ", "kw"):
            return kw or ""
        src, idx = (kw_nums, tok)
        if tok[:1] in ("ค", "m"):
            src, idx = msg_nums, tok[1:]
        if idx.isdigit():
            i = int(idx) - 1
            if 0 <= i < len(src):
                return src[i]
            return ""
        return m.group(0)

    return re.sub(r"\{([^}]{1,6})\}", pick, text)


def _split_keywords(val) -> list:
    """คำที่จับใส่ได้หลายคำในช่องเดียว — ขึ้นบรรทัดใหม่ หรือคั่นด้วย | (หรือส่งมาเป็น list ก็ได้)
    เช่น  กะเช้า(08.00-20.00 น.)
          กะเช้า(08:00-20:00 น.)
          กะเช้ารอบ
    คำไหนเจอก่อนก็ถือว่าตรง ใช้พูดได้เลย"""
    if isinstance(val, (list, tuple)):
        items = list(val)
    else:
        items = re.split(r"[\n\r|]+", str(val or ""))
    out, seen = [], set()
    for it in items:
        it = str(it).strip()
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


async def start_telegram():
    await tg_client.start()
    me = await tg_client.get_me()
    print(f"✅ Telegram ล็อกอิน: {me.first_name}", flush=True)

    @tg_client.on(events.Raw)
    async def _raw_probe(update):
        """ตัวดักสัญญาณดิบ — ถ้าบรรทัดนี้ขยับแต่ข้อความกลุ่มไม่ขยับ แปลว่าสายไม่ขาด
        แต่ข้อความถูกกรองทิ้ง (เช่น ไม่ได้อยู่ในกลุ่มนั้น / กลุ่มไม่ตรง)"""
        global _tg_raw_count, _last_tg_update
        _tg_raw_count += 1
        _last_tg_update = time.time()

    @tg_client.on(events.NewMessage())
    @tg_client.on(events.MessageEdited())      # บอท Telegram หลายตัวส่งข้อความเปล่าแล้วค่อยแก้เนื้อหา
    async def handler(event):
        global _last_tg_update, _tg_msg_count
        _last_tg_update = time.time()          # มีสัญญาณชีพจาก Telegram แล้ว
        _tg_msg_count += 1
        raw = (event.raw_text or "").strip()
        if not raw or not (event.is_group or event.is_channel):
            return
        raw_n = _norm_text(raw)

        # กันพูดซ้ำ — ยึดที่ "ข้อความชิ้นนี้เคยทำให้บอทพูดไปแล้วหรือยัง"
        #  • เคยพูดไปแล้ว → แก้ข้อความอีกกี่รอบก็ไม่พูดซ้ำ
        #  • ยังไม่เคยพูด (เช่น พิมพ์คำผิดตอนแรก) → พอแก้ให้ถูก บอทจะจับได้และพูด 1 ครั้ง
        try:
            mkey = (int(event.chat_id), int(event.id))
        except Exception:
            mkey = None
        if mkey:
            nowts = time.time()
            for k, v in list(_seen_msgs.items()):
                if nowts - v["ts"] > 3600:
                    _seen_msgs.pop(k, None)
            rec = _seen_msgs.get(mkey)
            if rec and rec["spoke"]:
                return
            if rec and rec.get("text") == raw_n[:300]:
                return                      # เนื้อหาเดิมเป๊ะ เข้ามาซ้ำ ไม่ต้องอ่านใหม่
            _seen_msgs[mkey] = {"ts": nowts, "spoke": False, "text": raw_n[:300]}

        try:
            chat = await event.get_chat()
            title = getattr(chat, "title", "") or ""
        except Exception:
            title = ""

        now_hm = _hm()
        # กันพูดซ้ำขั้นต่ำ 90 วินาที สำหรับ "ประกาศเนื้อหาเดียวกันเป๊ะ" เสมอ
        # (ถึงจะปิดกันซ้ำในเว็บไว้ก็ตาม — กันกรณีประกาศถูกส่งซ้ำ/ลบแล้วส่งใหม่ ซึ่งได้ message id ใหม่
        #  ทำให้ตัวกันซ้ำระดับข้อความจับไม่ได้ แล้วบอทพูด 2 รอบซ้อน)
        dedupe_sec = max(int(cfg.get("dedupe_seconds") or 0), 90)

        for grp in cfg.get("groups") or []:
            gid = str(grp.get("telegram_group_id") or "").strip()
            gname = (grp.get("telegram_group") or "").strip()
            matched = False
            if gid:
                if str(event.chat_id) == gid:
                    matched = True
            elif gname and gname in title:
                matched = True
            if not matched:
                continue
            hit_any = False
            tried = []
            for sh in grp.get("shifts") or []:
                if not sh.get("enabled"):
                    continue
                # ใส่ได้หลายคำ: ขึ้นบรรทัดใหม่ หรือคั่นด้วย | — เจอคำไหนก่อนก็พูด
                kw_list = _split_keywords(sh.get("keyword"))
                if not kw_list:
                    continue
                kw_i, kw, kw_nums = -1, None, []
                for i, k in enumerate(kw_list):
                    hit, nums = _kw_match(k, raw, raw_n)
                    if hit:
                        kw_i, kw, kw_nums = i, k, nums
                        break
                if not kw:
                    tried.extend(kw_list)
                    continue
                # ข้อความเฉพาะของคำนี้ (ถ้าเปิด "ข้อความแยกตามคำ" ไว้ในเว็บ)
                kw_text = ""
                if sh.get("text_per_kw"):
                    kt = sh.get("kw_texts") or []
                    if 0 <= kw_i < len(kt):
                        kw_text = (kt[kw_i] or "").strip()
                # ช่วงเวลาทำงานของกะ
                if not _in_window(now_hm, (sh.get("active_start") or "").strip(), (sh.get("active_end") or "").strip()):
                    print(f"[ข้ามนอกเวลา] กะ='{sh.get('name')}' ({sh.get('active_start')}-{sh.get('active_end')})", flush=True)
                    continue
                # กันจับซ้ำ
                dk = (gname, sh.get("name"), kw, tuple(kw_nums), raw_n[:200])
                if dedupe_sec > 0 and (time.time() - _dedupe.get(dk, 0)) < dedupe_sec:
                    print(f"[ข้ามซ้ำ] กะ='{sh.get('name')}' คำ='{kw}'", flush=True)
                    continue
                _dedupe[dk] = time.time()

                repeat = max(1, int(sh.get("repeat") or 1))
                voice = sh.get("voice_name") or "th-TH-PremwadeeNeural"
                triples = _shift_rooms(sh)
                if not triples:
                    continue
                n_bots = len(_online_units()) or 1
                print(f"[ตรงคำ] กลุ่ม='{title}' กะ='{sh.get('name')}' คำ='{kw}' ห้อง={len(triples)} รอบ={repeat} บอทออนไลน์={n_bots}", flush=True)
                jobs = [{
                    "room_id": rid, "text": _fill_placeholders(kw_text or txt, kw, raw, kw_nums), "voice_name": voice,
                    "repeat": repeat, "bot_id": bid,
                    "log": {"kind": "จับคำ", "group": title, "shift": sh.get("name"), "keyword": kw},
                } for rid, txt, bid in triples]
                hit_any = True
                if mkey:
                    _seen_msgs.setdefault(mkey, {"ts": time.time(), "text": raw_n[:300]})["spoke"] = True
                await dispatch_jobs(jobs)

            if not hit_any and tried:
                who = "?"
                try:
                    snd = await event.get_sender()
                    who = getattr(snd, "username", None) or getattr(snd, "first_name", None) or "?"
                    if getattr(snd, "bot", False):
                        who += " (bot)"
                except Exception:
                    pass
                print(f"[ไม่ตรงคำ] กลุ่ม='{title}' จาก={who} คำที่ตั้งไว้={tried} | ข้อความ: {raw_n[:80]}", flush=True)

    global _last_tg_update
    _last_tg_update = time.time()
    if not telegram_watchdog.is_running():
        telegram_watchdog.start()
    if not telegram_pulse.is_running():
        telegram_pulse.start()
    print("✅ พร้อมฟังข้อความแล้ว", flush=True)


# ---------- main ----------
async def main():
    # โหลด config ก่อนสตาร์ท (เผื่อมีบอทเสริมตั้งไว้แล้ว จะได้ขึ้นพร้อมกัน)
    first = _sb_get_setting("tts_voice_config")
    if isinstance(first, dict):
        cfg.update(first)

    # 🤖 บอทหลักจาก ENV (id = "main") — พฤติกรรมเดิม 100%
    main_unit = BotUnit("main", "บอทหลัก", DISCORD_TOKEN, is_main=True)
    FLEET["main"] = main_unit
    main_unit.start()

    # บอทเสริมจากหน้าเว็บ
    await sync_fleet()

    # loops กลาง (ผูกกับ loop หลัก ไม่ผูกกับ client ตัวใดตัวหนึ่ง)
    config_loop.start()
    status_loop.start()
    schedule_loop.start()
    telegram_list_loop.start()

    await start_telegram()
    await tg_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
