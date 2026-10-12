# -*- coding: utf-8 -*-
"""
MASTER OFFICIAL - Dashboard Server (IND Region)
2-Key System: Admin (full control) + User (read-only + add/mode)
Auto-Switch: Lv1-2 = BR | Lv3+ = LW (manual override allowed)
"""

import asyncio
import json
import os
import time
import secrets
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from aiohttp import web


# ==================== CONFIG ====================
ADMIN_KEY = os.environ.get("ADMIN_KEY", "TOXICFF")
USER_KEY = os.environ.get("USER_KEY", "freebytoxic")
TELEGRAM_LINK = "https://t.me/MASTER_FF_01"
SESSION_COOKIE = "master_session"
SESSION_TTL = 86400 * 7
BULK_STAGGER_DELAY = 1.5
AUTO_LW_LEVEL = 3

# ✅ User can: add, bulk_add, mode, refresh
# 🚫 User CANNOT: delete, pause, resume, pause_till_4am, pause_all, clear logs
ADMIN_ONLY_PATHS = {
    "/api/account/delete",
    "/api/account/pause",
    "/api/account/pause_till_4am",
    "/api/account/resume_now",
    "/api/account/pause_all",
    "/api/logs/clear",
}


# ==================== SESSION STORE ====================
_sessions: Dict[str, Dict[str, Any]] = {}


def _create_session(role: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"role": role, "expiry": time.time() + SESSION_TTL}
    return token


def _get_session(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    entry = _sessions.get(token)
    if not entry:
        return None
    if time.time() > entry.get("expiry", 0):
        _sessions.pop(token, None)
        return None
    return entry


def _validate_session(token: Optional[str]) -> bool:
    return _get_session(token) is not None


def _get_role(token: Optional[str]) -> str:
    entry = _get_session(token)
    return entry.get("role", "user") if entry else "guest"


def _destroy_session(token: Optional[str]):
    if token:
        _sessions.pop(token, None)


def _next_4am_timestamp() -> float:
    now = datetime.now()
    target = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target.timestamp()


# ==================== BOT STATE ====================
class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {"time": time.strftime("%H:%M:%S"), "level": level, "message": message, "uid": uid}
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def register_account(self, uid, nickname, region, level, exp, likes=0):
        uid_str = str(uid)
        lvl_val = max(1, int(level or 1))
        now = time.time()
        if uid_str not in self.accounts:
            auto_mode = "LONE_WOLF" if lvl_val >= AUTO_LW_LEVEL else "BR"
            self.accounts[uid_str] = {
                "uid": uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "IND",
                "level": lvl_val,
                "initial_exp": exp, "current_exp": exp, "gained_exp": 0,
                "likes": likes or 0,
                "mode": auto_mode, "mode_manual": False,
                "status": "ONLINE", "is_paused": False,
                "matches_played": 0, "matches_completed": 0, "active_matches": 0,
                "uptime_seconds": 0, "login_start_time": now,
                "consecutive_failures": 0,
                "pause_until": None, "pause_till_4am": False,
                "last_match_time": None, "last_match_failed": None,
                "last_updated": time.strftime("%H:%M:%S"),
                "_last_lvl": lvl_val,
            }
        else:
            acc = self.accounts[uid_str]
            if nickname: acc["nickname"] = nickname
            if region: acc["region"] = "IND"
            if level: acc["level"] = lvl_val
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            if not acc.get("mode_manual", False):
                old_lvl = int(acc.get("_last_lvl", 1) or 1)
                acc["mode"] = "LONE_WOLF" if lvl_val >= AUTO_LW_LEVEL else "BR"
                if old_lvl < AUTO_LW_LEVEL <= lvl_val:
                    self.log(
                        f"{acc.get('nickname','Unknown')} reached Lv{lvl_val} — auto switched to LONE WOLF",
                        "success", uid_str
                    )
                acc["_last_lvl"] = lvl_val
            acc["status"] = "ONLINE"
            acc["login_start_time"] = now
            acc["consecutive_failures"] = 0
            acc["last_updated"] = time.strftime("%H:%M:%S")
        self.recalc_totals()

    def update_exp(self, uid, current_exp, level=None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old = acc["current_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                old_lvl = int(acc.get("level", 1) or 1)
                acc["level"] = int(level)
                if not acc.get("mode_manual", False):
                    new_lvl = int(level)
                    acc["mode"] = "LONE_WOLF" if new_lvl >= AUTO_LW_LEVEL else "BR"
                    if old_lvl < AUTO_LW_LEVEL <= new_lvl:
                        self.log(
                            f"{acc.get('nickname','Unknown')} reached Lv{new_lvl} — auto switched to LONE WOLF",
                            "success", uid_str
                        )
                    acc["_last_lvl"] = new_lvl
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = time.strftime("%H:%M:%S")
            d = current_exp - old
            if d > 0:
                self.log(f"{acc['nickname']} ({uid_str}) +{d} EXP! Total: +{acc['gained_exp']}", "success", uid_str)
            self.recalc_totals()

    def get_account_level(self, uid):
        acc = self.accounts.get(str(uid))
        return int(acc.get("level", 1) or 1) if acc else 1

    def get_account_mode(self, uid):
        acc = self.accounts.get(str(uid))
        if not acc:
            return "BR"
        if acc.get("mode_manual", False):
            mode = str(acc.get("mode", "BR") or "BR").upper()
            return "LONE_WOLF" if mode == "LONE_WOLF" else "BR"
        level = int(acc.get("level", 1) or 1)
        return "LONE_WOLF" if level >= AUTO_LW_LEVEL else "BR"

    def set_account_mode(self, uid, mode):
        uid_str = str(uid)
        mode = str(mode or "").upper().replace("-", "_").replace(" ", "_")
        if mode in ("LW", "LONEWOLF", "LONE_WOLF"):
            mode = "LONE_WOLF"
        elif mode == "BR":
            mode = "BR"
        else:
            return False, "Invalid mode"
        acc = self.accounts.get(uid_str)
        if not acc:
            return False, "Account not found"
        level = int(acc.get("level", 1) or 1)
        if level < AUTO_LW_LEVEL and mode != "BR":
            return False, f"Lone Wolf is locked until Level {AUTO_LW_LEVEL}"
        acc["mode"] = mode
        acc["mode_manual"] = True
        acc["last_updated"] = time.strftime("%H:%M:%S")
        self.log(f"{acc.get('nickname','Unknown')} ({uid_str}) switched to {mode} (manual)", "success", uid_str)
        return True, mode

    def update_status(self, uid, status, active_matches=None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def is_paused(self, uid):
        acc = self.accounts.get(str(uid))
        if not acc: return False
        return bool(acc.get("is_paused", False) or acc.get("status") == "PAUSED")

    def set_paused(self, uid, paused):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["is_paused"] = bool(paused)
            self.accounts[uid_str]["status"] = "PAUSED" if paused else "ONLINE"
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")
            return True
        return False

    def increment_match_started(self, uid):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_played"] = self.accounts[uid_str].get("matches_played", 0) + 1
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def increment_match(self, uid):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_completed"] = self.accounts[uid_str].get("matches_completed", 0) + 1
            self.accounts[uid_str]["last_match_time"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["consecutive_failures"] = 0

    def match_failed(self, uid):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["last_match_failed"] = time.strftime("%H:%M:%S")
            self.accounts[uid_str]["last_updated"] = time.strftime("%H:%M:%S")

    def mark_login_failure(self, uid):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["consecutive_failures"] = self.accounts[uid_str].get("consecutive_failures", 0) + 1
            fails = self.accounts[uid_str]["consecutive_failures"]
            if fails >= 10:
                self.accounts[uid_str]["status"] = "DISABLED"
                self.accounts[uid_str]["is_paused"] = True
                self.log(f"{uid_str} disabled after {fails} failures", "error", uid_str)

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())


bot_state = BotState()


# ==================== PATHS ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "index.html")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")


# ==================== LOGIN PAGE ====================
LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#05060f">
<title>MASTER | Secure Access</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Orbitron:wght@500;700;900&family=Cinzel:wght@600;700;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.7.2/css/all.min.css">
<style>
:root{--bg:#05060f;--a1:#60a5fa;--a2:#2563eb;--a3:#93c5fd;--line:rgba(96,165,250,.16);--line2:rgba(96,165,250,.45);--text:#f0f3ff;--text2:#a8b0d8;--text3:#7278a0;--err:#fb7185;--ok:#4ade80}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden;font-family:'Inter',system-ui,sans-serif;-webkit-font-smoothing:antialiased}
body{background:radial-gradient(ellipse at 50% 30%,#0a0d24 0%,#04050c 70%);color:var(--text);display:grid;place-items:center;padding:20px;position:relative}
.bg-layer{position:fixed;inset:0;overflow:hidden;pointer-events:none;z-index:0}
.bg-layer::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 60% 50% at 15% 0%,rgba(96,165,250,.28),transparent 60%),radial-gradient(ellipse 55% 50% at 85% 100%,rgba(168,85,247,.22),transparent 60%),radial-gradient(ellipse 50% 60% at 50% 50%,rgba(236,72,153,.1),transparent 65%);animation:bgBreath 8s ease-in-out infinite}
@keyframes bgBreath{0%,100%{opacity:.85;transform:scale(1)}50%{opacity:1;transform:scale(1.05)}}
.bg-grid{position:absolute;inset:0;background-image:linear-gradient(rgba(96,165,250,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(96,165,250,.045) 1px,transparent 1px);background-size:56px 56px;mask-image:radial-gradient(ellipse at 50% 45%,#000 25%,transparent 78%);-webkit-mask-image:radial-gradient(ellipse at 50% 45%,#000 25%,transparent 78%)}
.orb{position:absolute;border-radius:50%;filter:blur(110px);opacity:.55;pointer-events:none}
.orb.o1{width:420px;height:420px;background:radial-gradient(circle,#3b82f6,transparent 70%);top:-140px;left:-140px;animation:orb1 16s ease-in-out infinite}
.orb.o2{width:380px;height:380px;background:radial-gradient(circle,#7c3aed,transparent 70%);bottom:-120px;right:-120px;animation:orb2 18s ease-in-out infinite}
.orb.o3{width:280px;height:280px;background:radial-gradient(circle,#ec4899,transparent 70%);top:40%;right:-60px;opacity:.35;animation:orb1 22s ease-in-out infinite reverse}
@keyframes orb1{50%{transform:translate(50px,50px) scale(1.15)}}
@keyframes orb2{50%{transform:translate(-50px,-50px) scale(1.15)}}
.wrap{position:relative;z-index:2;width:100%;max-width:410px;perspective:1000px}
.card{position:relative;border-radius:26px;overflow:hidden;background:linear-gradient(180deg,rgba(18,22,44,.94),rgba(8,10,22,.97));border:1px solid var(--line2);box-shadow:0 40px 100px rgba(0,0,0,.75),0 0 80px rgba(96,165,250,.18),inset 0 1px 0 rgba(255,255,255,.08),inset 0 -1px 0 rgba(0,0,0,.35);backdrop-filter:blur(26px) saturate(180%);-webkit-backdrop-filter:blur(26px) saturate(180%);animation:cardIn .7s cubic-bezier(.16,1,.3,1);transition:transform .35s cubic-bezier(.2,.8,.2,1)}
@keyframes cardIn{from{opacity:0;transform:translateY(30px) scale(.97)}}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,#93c5fd 25%,#a855f7 50%,#93c5fd 75%,transparent);background-size:300% 100%;animation:textFlow 5s linear infinite;filter:drop-shadow(0 0 8px var(--a1))}
@keyframes textFlow{to{background-position:300% 0}}
.card-top-glow{position:absolute;top:-2px;left:50%;transform:translateX(-50%);width:70%;height:140px;pointer-events:none;background:radial-gradient(ellipse at top,rgba(96,165,250,.3),transparent 70%);filter:blur(24px)}
.card-inner{padding:38px 30px 26px;position:relative}
.hero{text-align:center;margin-bottom:24px;position:relative}
.seal{width:90px;height:90px;margin:0 auto 20px;position:relative;display:flex;align-items:center;justify-content:center;border-radius:50%;background:radial-gradient(circle at 30% 30%,rgba(255,255,255,.1),transparent 60%),linear-gradient(135deg,#0b0f1e,#050810);box-shadow:inset 0 0 0 1.5px rgba(96,165,250,.5),inset 0 0 24px rgba(96,165,250,.2),0 0 40px rgba(96,165,250,.45),0 0 80px rgba(96,165,250,.15);isolation:isolate}
.seal::before{content:'';position:absolute;inset:-5px;border-radius:50%;padding:2px;background:conic-gradient(from -45deg,transparent 0deg,#93c5fd 60deg,#a855f7 140deg,transparent 200deg,transparent 360deg);-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude;animation:ringSpin 6s linear infinite;filter:drop-shadow(0 0 10px rgba(96,165,250,.9))}
@keyframes ringSpin{to{transform:rotate(360deg)}}
.seal::after{content:'M';font-family:'Cinzel',serif;font-weight:900;font-size:46px;line-height:1;background:linear-gradient(180deg,#ffffff 0%,#93c5fd 40%,#60a5fa 70%,#2563eb 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;filter:drop-shadow(0 0 14px rgba(96,165,250,.7));z-index:2;position:relative;animation:mShine 4s ease-in-out infinite}
@keyframes mShine{0%,100%{filter:drop-shadow(0 0 14px rgba(96,165,250,.7)) drop-shadow(0 2px 4px rgba(0,0,0,.9))}50%{filter:drop-shadow(0 0 22px rgba(147,197,253,1)) drop-shadow(0 0 36px rgba(96,165,250,.7)) drop-shadow(0 2px 4px rgba(0,0,0,.9))}}
.dot-glow{position:absolute;inset:0;border-radius:50%;pointer-events:none;background:radial-gradient(circle,rgba(96,165,250,.2) 0%,transparent 60%);animation:glowPulse 4s ease-in-out infinite}
@keyframes glowPulse{0%,100%{opacity:.6}50%{opacity:1}}
.brand-title{font-family:'Cinzel',serif;font-weight:900;font-size:23px;letter-spacing:2px;line-height:1.1;background:linear-gradient(180deg,#ffffff 0%,#c8dcff 55%,#7ea8e8 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;filter:drop-shadow(0 2px 14px rgba(96,165,250,.4));margin-bottom:6px}
.brand-sub{font-family:'Orbitron',sans-serif;font-size:9px;font-weight:700;letter-spacing:5px;color:var(--a1);text-transform:uppercase;opacity:.88;margin-bottom:14px}
.status-badge{display:inline-flex;align-items:center;gap:7px;padding:5px 13px;border-radius:99px;background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.35);font-family:'Orbitron',sans-serif;font-size:8px;font-weight:800;letter-spacing:2.5px;color:#6ee7b7;text-transform:uppercase;box-shadow:0 0 20px rgba(74,222,128,.15)}
.status-badge .dot{width:5px;height:5px;border-radius:50%;background:#4ade80;box-shadow:0 0 8px #4ade80;animation:pulseDot 2s infinite}
@keyframes pulseDot{50%{opacity:.4;transform:scale(1.4)}}
.greeting{font-size:10px;font-weight:700;color:var(--text3);letter-spacing:2.5px;text-transform:uppercase;text-align:center;margin-bottom:6px}
.form-heading{font-size:20px;font-weight:800;letter-spacing:-.3px;text-align:center;margin-bottom:6px;background:linear-gradient(180deg,#ffffff 0%,#c8dcff 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.form-desc{text-align:center;font-size:11.5px;color:var(--text2);margin-bottom:22px;line-height:1.5;font-weight:500}
.field{position:relative;margin-bottom:14px}
.field .input-wrap{position:relative;border-radius:14px;padding:1.5px;background:linear-gradient(135deg,rgba(96,165,250,.3),rgba(168,85,247,.2),rgba(96,165,250,.3));background-size:300% 300%;transition:all .35s cubic-bezier(.2,.8,.2,1)}
.field .input-wrap::before{content:'';position:absolute;inset:0;border-radius:14px;background:linear-gradient(135deg,var(--a1),#a855f7,var(--a1));background-size:300% 300%;opacity:0;transition:opacity .35s;animation:borderFlow 3s linear infinite}
@keyframes borderFlow{to{background-position:300% 300%}}
.field.focused .input-wrap::before{opacity:1}
.field .input-inner{position:relative;border-radius:12.5px;background:linear-gradient(180deg,rgba(8,10,22,.98),rgba(5,7,16,.98));overflow:hidden}
.field input{width:100%;padding:16px 48px 16px 46px;border:0;background:transparent;color:var(--text);font-size:14px;font-family:'JetBrains Mono',monospace;letter-spacing:3px;outline:none;caret-color:transparent;position:relative;z-index:2}
.field input::placeholder{letter-spacing:.5px;font-family:'Inter',sans-serif;color:var(--text3);font-size:12.5px;font-weight:500}
.field .ico-l{position:absolute;left:16px;top:50%;transform:translateY(-50%);color:var(--text2);font-size:14px;pointer-events:none;transition:color .25s;z-index:3}
.field.focused .ico-l{color:var(--a1)}
.field .ico-r{position:absolute;right:16px;top:50%;transform:translateY(-50%);color:var(--text2);font-size:14px;cursor:pointer;transition:color .25s;user-select:none;z-index:3}
.field .ico-r:hover{color:var(--a1)}
.field .scan-line{position:absolute;top:0;bottom:0;width:2px;background:linear-gradient(180deg,transparent,var(--a3),transparent);box-shadow:0 0 12px var(--a1),0 0 24px var(--a1);opacity:0;pointer-events:none;z-index:1;transition:opacity .2s}
.field.typing .scan-line{opacity:1;animation:scanSweep 1.2s ease-out}
@keyframes scanSweep{0%{left:0;opacity:1}90%{left:100%;opacity:1}100%{left:100%;opacity:0}}
.field.focused .input-wrap{box-shadow:0 0 0 3px rgba(96,165,250,.14),0 0 32px rgba(96,165,250,.25)}
.field input:not(:placeholder-shown){text-shadow:0 0 8px rgba(96,165,250,.5)}
.field .corner{position:absolute;width:10px;height:10px;border:1.5px solid var(--a1);opacity:0;transition:opacity .3s,transform .3s;pointer-events:none;z-index:3}
.field .corner.tl{top:4px;left:4px;border-right:0;border-bottom:0;border-radius:6px 0 0 0}
.field .corner.tr{top:4px;right:4px;border-left:0;border-bottom:0;border-radius:0 6px 0 0}
.field .corner.bl{bottom:4px;left:4px;border-right:0;border-top:0;border-radius:0 0 0 6px}
.field .corner.br{bottom:4px;right:4px;border-left:0;border-top:0;border-radius:0 0 6px 0}
.field.focused .corner{opacity:1;transform:scale(1.1)}
.btn{width:100%;padding:16px;border-radius:14px;border:0;cursor:pointer;font-family:'Inter',sans-serif;font-weight:800;font-size:12.5px;letter-spacing:1.5px;color:#fff;text-transform:uppercase;background:linear-gradient(180deg,#6aa8ff 0%,#3b82f6 50%,#1e40af 100%);box-shadow:0 12px 34px rgba(59,130,246,.5),0 4px 12px rgba(0,0,0,.35),inset 0 1px 0 rgba(255,255,255,.4),inset 0 -1px 0 rgba(0,0,0,.2);transition:all .2s cubic-bezier(.34,1.56,.64,1);position:relative;overflow:hidden;display:flex;align-items:center;justify-content:center;gap:9px;margin-top:6px}
.btn:hover{transform:translateY(-2px);box-shadow:0 18px 42px rgba(59,130,246,.65),0 4px 12px rgba(0,0,0,.35),inset 0 1px 0 rgba(255,255,255,.5)}
.btn:active{transform:translateY(0) scale(.99)}
.btn:disabled{opacity:.6;cursor:not-allowed;transform:none!important}
.btn::before{content:'';position:absolute;top:0;left:-100%;width:100%;height:100%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.28),transparent);animation:btnShine 4s ease-in-out infinite}
@keyframes btnShine{0%{left:-100%}60%{left:100%}100%{left:100%}}
.btn i{font-size:13px}
.btn-tg{width:100%;padding:13px;border-radius:14px;margin-top:10px;border:1px solid var(--line);background:rgba(10,16,32,.6);color:var(--text);cursor:pointer;text-decoration:none;font-family:'Inter',sans-serif;font-weight:700;font-size:12px;letter-spacing:1px;display:flex;align-items:center;justify-content:center;gap:9px;transition:all .25s cubic-bezier(.2,.8,.2,1)}
.btn-tg:hover{border-color:var(--line2);background:rgba(96,165,250,.1);transform:translateY(-1px);box-shadow:0 0 28px rgba(96,165,250,.2)}
.btn-tg i{font-size:15px;color:#229ED9}
.msg{margin-top:14px;padding:11px 14px;border-radius:11px;font-size:12px;font-weight:600;display:none;align-items:center;gap:9px;line-height:1.4;animation:msgIn .3s}
@keyframes msgIn{from{opacity:0;transform:translateY(-6px)}}
.msg.err{display:flex;background:rgba(251,113,133,.12);border:1px solid rgba(251,113,133,.4);color:#fca5a5}
.msg.ok{display:flex;background:rgba(74,222,128,.12);border:1px solid rgba(74,222,128,.4);color:#6ee7b7}
.msg i{font-size:13px}
.shake{animation:shake .5s cubic-bezier(.36,.07,.19,.97)}
@keyframes shake{10%,90%{transform:translateX(-2px)}20%,80%{transform:translateX(4px)}30%,50%,70%{transform:translateX(-6px)}40%,60%{transform:translateX(6px)}}
.sec-row{display:flex;justify-content:center;gap:6px;margin-top:18px;flex-wrap:wrap}
.sec-item{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:8px;background:rgba(96,165,250,.06);border:1px solid rgba(96,165,250,.15);font-size:9.5px;font-weight:700;letter-spacing:.5px;color:var(--text2);transition:all .25s}
.sec-item i{font-size:9px;color:var(--a1);opacity:.9}
.sec-item:hover{border-color:var(--line2);color:var(--text3)}
.divider{height:1px;margin:22px 0 16px;background:linear-gradient(90deg,transparent,var(--line),transparent)}
.foot{display:flex;flex-direction:column;align-items:center;gap:6px;font-size:9.5px;font-weight:600;color:var(--text3);letter-spacing:1.5px;text-transform:uppercase}
.foot-row{display:flex;align-items:center;gap:8px}
.foot-row .sep{color:var(--a1);opacity:.45;font-size:6px}
.foot-row .hl{color:var(--a1);font-weight:800}
.foot-row .tg{color:#5b9eff;display:inline-flex;align-items:center;gap:4px}
.foot-row .tg i{font-size:9px;color:#229ED9}
.v-chip{position:absolute;top:16px;right:16px;padding:3px 9px;border-radius:7px;font-family:'Orbitron',sans-serif;font-size:8px;font-weight:900;letter-spacing:1px;color:#1a1300;background:linear-gradient(135deg,#fcd34d,#fbbf24 50%,#b45309);box-shadow:0 3px 10px rgba(251,191,36,.5),inset 0 1px 0 rgba(255,255,255,.5);border:1px solid rgba(255,255,255,.3);z-index:5}
@media(max-width:480px){body{padding:14px}.card-inner{padding:32px 20px 22px}.seal{width:78px;height:78px;margin-bottom:16px}.seal::after{font-size:40px}.brand-title{font-size:20px;letter-spacing:1.5px}.form-heading{font-size:18px}.btn{font-size:11.5px;padding:14px}.v-chip{top:12px;right:12px;font-size:7px}.orb{filter:blur(80px)}.field input{padding:15px 44px 15px 44px;font-size:13.5px;letter-spacing:2.5px}}
@media(max-width:360px){.card-inner{padding:28px 16px 20px}.brand-title{font-size:18px}.form-heading{font-size:16px}.form-desc{font-size:11px}}
</style>
</head>
<body>
<div class="bg-layer"><div class="bg-grid"></div><div class="orb o1"></div><div class="orb o2"></div><div class="orb o3"></div></div>
<div class="wrap">
  <div class="card" id="mainCard">
    <div class="card-top-glow"></div>
    <div class="card-inner">
      <div class="v-chip">v0.2</div>
      <div class="hero">
        <div class="seal"><div class="dot-glow"></div></div>
        <div class="brand-title">MASTER OFFICIAL</div>
        <div class="brand-sub">Command Center</div>
        <div class="status-badge"><span class="dot"></span>Secure Access</div>
      </div>
      <div class="greeting" id="greeting">Welcome</div>
      <div class="form-heading">Access Command Center</div>
      <div class="form-desc">Enter your access key to continue</div>
      <form id="loginForm" autocomplete="off">
        <div class="field" id="keyField">
          <div class="input-wrap">
            <div class="input-inner">
              <span class="corner tl"></span>
              <span class="corner tr"></span>
              <span class="corner bl"></span>
              <span class="corner br"></span>
              <span class="scan-line"></span>
              <input type="password" id="keyInput" placeholder="Enter access key" autocomplete="new-password" autofocus>
              <i class="fa-solid fa-key ico-l"></i>
              <i class="fa-solid fa-eye ico-r" id="eyeToggle"></i>
            </div>
          </div>
        </div>
        <button type="submit" class="btn" id="loginBtn"><i class="fa-solid fa-shield-halved"></i><span>Unlock Level Up System</span></button>
        <a class="btn-tg" href="{{TG_LINK}}" target="_blank" rel="noopener"><i class="fa-brands fa-telegram"></i><span>Contact Owner</span></a>
        <div class="msg" id="msg"></div>
        <div class="sec-row">
          <span class="sec-item"><i class="fa-solid fa-lock"></i>256-bit SSL</span>
          <span class="sec-item"><i class="fa-solid fa-shield"></i>Encrypted</span>
          <span class="sec-item"><i class="fa-solid fa-fingerprint"></i>Secure</span>
        </div>
      </form>
      <div class="divider"></div>
      <div class="foot">
        <div class="foot-row"><span>Protected Access</span><span class="sep">●</span><span class="hl">Premium Engine</span></div>
        <div class="foot-row"><span class="tg"><i class="fa-brands fa-telegram"></i>@MASTER_FF_01</span><span class="sep">●</span><span>Level Up System</span></div>
      </div>
    </div>
  </div>
</div>
<script>
var input=document.getElementById('keyInput');var eye=document.getElementById('eyeToggle');var form=document.getElementById('loginForm');var msg=document.getElementById('msg');var btn=document.getElementById('loginBtn');var card=document.getElementById('mainCard');var keyField=document.getElementById('keyField');
(function(){var h=new Date().getHours();var g='Welcome';if(h<12)g='Good Morning';else if(h<18)g='Good Afternoon';else g='Good Evening';document.getElementById('greeting').textContent=g})();
input.addEventListener('focus',function(){keyField.classList.add('focused')});
input.addEventListener('blur',function(){keyField.classList.remove('focused')});
var scanTimer=null;
input.addEventListener('input',function(){keyField.classList.remove('typing');void keyField.offsetWidth;keyField.classList.add('typing');clearTimeout(scanTimer);scanTimer=setTimeout(function(){keyField.classList.remove('typing')},1200)});
eye.addEventListener('click',function(){if(input.type==='password'){input.type='text';eye.className='fa-solid fa-eye-slash ico-r'}else{input.type='password';eye.className='fa-solid fa-eye ico-r'}});
form.addEventListener('submit',async function(e){e.preventDefault();var key=input.value.trim();if(!key){show('Please enter access key','err');shake();return}btn.disabled=true;btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i><span>Verifying...</span>';try{var r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:key})});var d=await r.json();if(d.status==='ok'){show('Access granted as '+(d.role||'user'),'ok');btn.innerHTML='<i class="fa-solid fa-check"></i><span>Success</span>';setTimeout(function(){window.location.href='/'},700)}else{show(d.error||'Invalid access key','err');input.value='';input.focus();btn.disabled=false;btn.innerHTML='<i class="fa-solid fa-shield-halved"></i><span>Unlock Level Up System</span>';shake()}}catch(err){show('Connection failed. Try again.','err');btn.disabled=false;btn.innerHTML='<i class="fa-solid fa-shield-halved"></i><span>Unlock Level Up System</span>';shake()}});
function show(text,type){msg.className='msg '+type;msg.innerHTML='<i class="fa-solid '+(type==='err'?'fa-circle-xmark':'fa-circle-check')+'"></i><span>'+text+'</span>'}
function shake(){card.classList.remove('shake');void card.offsetWidth;card.classList.add('shake')}
if(window.innerWidth>=900){var wrap=card.parentElement;wrap.addEventListener('mousemove',function(e){var r=wrap.getBoundingClientRect();var x=(e.clientX-r.left)/r.width-0.5;var y=(e.clientY-r.top)/r.height-0.5;card.style.transform='rotateY('+(x*4)+'deg) rotateX('+(-y*4)+'deg)'});wrap.addEventListener('mouseleave',function(){card.style.transform=''})}
</script>
</body>
</html>""".replace("{{TG_LINK}}", TELEGRAM_LINK)


# ==================== AUTH MIDDLEWARE ====================
@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path
    if path in ("/login", "/api/login"):
        return await handler(request)
    token = request.cookies.get(SESSION_COOKIE)
    session = _get_session(token)
    if not session:
        if path.startswith("/api/"):
            return web.json_response({"status": "error", "error": "Unauthorized"}, status=401)
        return web.HTTPFound("/login")
    if path in ADMIN_ONLY_PATHS and session.get("role") != "admin":
        return web.json_response({"status": "error", "error": "Admin access required"}, status=403)
    return await handler(request)


# ==================== LOGIN HANDLERS ====================
async def handle_login_page(request):
    token = request.cookies.get(SESSION_COOKIE)
    if _validate_session(token):
        return web.HTTPFound("/")
    return web.Response(text=LOGIN_PAGE_HTML, content_type="text/html", charset="utf-8")


async def handle_login(request):
    try:
        data = await request.json()
        key = str(data.get("key", "")).strip()
        if not key:
            return web.json_response({"status": "error", "error": "Key required"}, status=400)
        role = None
        if key == ADMIN_KEY:
            role = "admin"
        elif key == USER_KEY:
            role = "user"
        else:
            bot_state.log(f"Failed login attempt from {request.remote}", "warning")
            return web.json_response({"status": "error", "error": "Invalid key"}, status=401)
        token = _create_session(role)
        resp = web.json_response({"status": "ok", "role": role})
        resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="Lax")
        bot_state.log(f"Login successful as {role.upper()}", "success")
        return resp
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_logout(request):
    token = request.cookies.get(SESSION_COOKIE)
    _destroy_session(token)
    resp = web.json_response({"status": "ok"})
    resp.del_cookie(SESSION_COOKIE)
    bot_state.log("User logged out", "info")
    return resp


async def handle_session_info(request):
    token = request.cookies.get(SESSION_COOKIE)
    session = _get_session(token)
    if not session:
        return web.json_response({"status": "error", "error": "Unauthorized"}, status=401)
    return web.json_response({
        "status": "ok",
        "role": session.get("role", "user"),
        "is_admin": session.get("role") == "admin",
        "auto_lw_level": AUTO_LW_LEVEL,
    })


# ==================== MAIN HANDLERS ====================
async def handle_index(request):
    if os.path.exists(TEMPLATE_PATH):
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = "<h1>templates/index.html not found!</h1>"
    return web.Response(text=content, content_type="text/html", charset="utf-8")


async def handle_get_stats(request):
    accounts_data = list(bot_state.accounts.values())
    accounts_data.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)
    now = time.time()
    for acc in accounts_data:
        start = acc.get("login_start_time", 0)
        acc["uptime_seconds"] = int(now - start) if start else 0
    return web.json_response({
        "total_accounts": len(bot_state.accounts),
        "total_matches": bot_state.total_matches,
        "total_gained_exp": bot_state.total_gained_exp,
        "accounts": accounts_data,
        "logs": bot_state.logs[-60:],
        "uptime": int(time.time() - bot_state.start_time),
        "auto_lw_level": AUTO_LW_LEVEL,
    })


async def handle_add_account(request):
    try:
        data = await request.json()
        existing = _load_accounts_file()
        if "uid" in data and "password" in data:
            uid = str(data["uid"]).strip()
            pwd = str(data["password"]).strip()
            if not uid or not pwd:
                return web.json_response({"status": "error", "error": "UID and Password required"})
            existing = [acc for acc in existing if str(acc.get("uid")) != uid]
            existing.append({"uid": uid, "password": pwd})
        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token required"})
            existing = [acc for acc in existing if acc.get("token") != token]
            existing.append({"token": token})
        else:
            return web.json_response({"status": "error", "error": "Invalid payload"})
        _save_accounts_file(existing)
        bot_state.log(f"New account added: {data.get('uid') or 'Token'}", "success")
        if "on_account_added" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_account_added"](data))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_bulk_add(request):
    try:
        data = await request.json()
        new_accounts = data.get("accounts", [])
        if not isinstance(new_accounts, list):
            return web.json_response({"status": "error", "error": "Invalid accounts list"})
        existing = _load_accounts_file()
        existing_keys = set()
        for acc in existing:
            if "uid" in acc and acc.get("uid"): existing_keys.add(f"uid:{acc['uid']}")
            elif "token" in acc and acc.get("token"): existing_keys.add(f"tok:{str(acc['token'])[:20]}")
        added = 0; skipped = 0; invalid = 0; added_entries = []
        for acc in new_accounts:
            if not isinstance(acc, dict): invalid += 1; continue
            if "uid" in acc and "password" in acc:
                uid = str(acc["uid"]).strip()
                pwd = str(acc["password"]).strip()
                if not uid or not pwd: invalid += 1; continue
                key = f"uid:{uid}"
                if key in existing_keys: skipped += 1; continue
                existing_keys.add(key)
                entry = {"uid": uid, "password": pwd}
                existing.append(entry); added_entries.append(entry); added += 1
            elif "token" in acc:
                token = str(acc["token"]).strip()
                if not token: invalid += 1; continue
                key = f"tok:{token[:20]}"
                if key in existing_keys: skipped += 1; continue
                existing_keys.add(key)
                entry = {"token": token}
                existing.append(entry); added_entries.append(entry); added += 1
            else: invalid += 1
        _save_accounts_file(existing)
        if added > 0:
            bot_state.log(f"Bulk add: +{added} accounts, {skipped} skipped, {invalid} invalid", "success")
            if "on_account_added" in bot_state.refresh_callbacks:
                asyncio.create_task(_staggered_worker_start(added_entries))
        return web.json_response({"status": "ok", "added": added, "skipped": skipped, "invalid": invalid})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def _staggered_worker_start(entries):
    callback = bot_state.refresh_callbacks.get("on_account_added")
    if not callback: return
    for i, entry in enumerate(entries):
        try:
            if i > 0: await asyncio.sleep(BULK_STAGGER_DELAY)
            await callback(entry)
        except Exception as e:
            bot_state.log(f"Worker start failed: {e}", "error")


async def handle_delete_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        token = str(data.get("token", "")).strip()
        existing = _load_accounts_file()
        if token: existing = [acc for acc in existing if str(acc.get("token", "")).strip() != token]
        elif uid: existing = [acc for acc in existing if str(acc.get("uid", "")).strip() != uid]
        _save_accounts_file(existing)
        worker_keys = []
        if uid: worker_keys.append(uid)
        if token: worker_keys.append(token[:10])
        for key in worker_keys:
            worker = bot_state.account_workers.get(key)
            if worker:
                if not worker.done(): worker.cancel()
                bot_state.account_workers.pop(key, None)
        if uid and uid in bot_state.accounts: del bot_state.accounts[uid]
        bot_state.log(f"Account {uid or token[:10] or 'unknown'} removed", "warning", uid or None)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_refresh_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if "on_refresh_account" in bot_state.refresh_callbacks:
            asyncio.create_task(bot_state.refresh_callbacks["on_refresh_account"](uid))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_mode_switch(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        mode = str(data.get("mode", "")).strip()
        ok, result = bot_state.set_account_mode(uid, mode)
        if not ok:
            return web.json_response({"status": "error", "error": result}, status=400)
        return web.json_response({"status": "ok", "uid": uid, "mode": result, "level": bot_state.get_account_level(uid)})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_bulk_mode_switch(request):
    try:
        data = await request.json()
        mode = str(data.get("mode", "BR")).strip()
        changed = 0; skipped = 0
        for uid in list(bot_state.accounts.keys()):
            ok, _ = bot_state.set_account_mode(uid, mode)
            if ok: changed += 1
            else: skipped += 1
        bot_state.log(f"Bulk mode switch to {mode}: {changed} changed, {skipped} skipped", "success")
        return web.json_response({"status": "ok", "changed": changed, "skipped": skipped, "mode": mode})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_pause_account(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"}, status=400)
        acc = bot_state.accounts.get(uid)
        if not acc:
            return web.json_response({"status": "error", "error": "Account not found"}, status=404)
        currently_paused = bot_state.is_paused(uid)
        new_state = not currently_paused
        bot_state.set_paused(uid, new_state)
        if not new_state:
            acc["pause_until"] = None
            acc["pause_till_4am"] = False
        bot_state.log(f"{acc.get('nickname','Unknown')} ({uid}) {'paused' if new_state else 'resumed'}",
                      "warning" if new_state else "success", uid)
        return web.json_response({"status": "ok", "uid": uid, "is_paused": new_state})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_pause_till_4am(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"}, status=400)
        acc = bot_state.accounts.get(uid)
        if not acc:
            return web.json_response({"status": "error", "error": "Account not found"}, status=404)
        until_ts = _next_4am_timestamp()
        acc["pause_until"] = until_ts
        acc["pause_till_4am"] = True
        bot_state.set_paused(uid, True)
        until_str = datetime.fromtimestamp(until_ts).strftime("%d %b %I:%M %p")
        bot_state.log(f"{acc.get('nickname','Unknown')} ({uid}) paused till {until_str}", "warning", uid)
        return web.json_response({"status": "ok", "uid": uid, "pause_until": until_ts})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_resume_now(request):
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"}, status=400)
        acc = bot_state.accounts.get(uid)
        if not acc:
            return web.json_response({"status": "error", "error": "Account not found"}, status=404)
        acc["pause_until"] = None
        acc["pause_till_4am"] = False
        bot_state.set_paused(uid, False)
        bot_state.log(f"{acc.get('nickname','Unknown')} ({uid}) manually resumed", "success", uid)
        return web.json_response({"status": "ok", "uid": uid})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_pause_all(request):
    try:
        if not bot_state.accounts:
            return web.json_response({"status": "error", "error": "No accounts"}, status=400)
        any_running = any(not bot_state.is_paused(uid) for uid in bot_state.accounts.keys())
        new_state = any_running
        for uid in bot_state.accounts.keys():
            bot_state.set_paused(uid, new_state)
            if not new_state:
                bot_state.accounts[uid]["pause_until"] = None
                bot_state.accounts[uid]["pause_till_4am"] = False
        bot_state.log(f"All accounts {'paused' if new_state else 'resumed'}",
                      "warning" if new_state else "success")
        return web.json_response({"status": "ok", "all_paused": new_state})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def handle_clear_logs(request):
    bot_state.logs.clear()
    return web.json_response({"status": "ok"})


# ==================== HELPERS ====================
def _load_accounts_file():
    if not os.path.exists(ACCOUNTS_FILE): return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_accounts_file(accounts):
    try:
        tmp = ACCOUNTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2)
        os.replace(tmp, ACCOUNTS_FILE)
    except Exception as e:
        print(f"Save accounts failed: {e}")


# ==================== AUTO RESUME TASK ====================
async def auto_resume_task():
    while True:
        try:
            now = time.time()
            for uid, acc in list(bot_state.accounts.items()):
                until = acc.get("pause_until")
                if until and now >= until:
                    acc["pause_until"] = None
                    acc["pause_till_4am"] = False
                    bot_state.set_paused(uid, False)
                    bot_state.log(
                        f"{acc.get('nickname','Unknown')} ({uid}) auto-resumed (4 AM)",
                        "success", uid
                    )
        except Exception as e:
            print(f"Auto-resume error: {e}")
        await asyncio.sleep(30)


# ==================== START SERVER ====================
async def start_web_dashboard(host="0.0.0.0", port=5000):
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/login", handle_login_page)
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/logout", handle_logout)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/session", handle_session_info)
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/bulk_add", handle_bulk_add)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/account/mode", handle_mode_switch)
    app.router.add_post("/api/account/bulk_mode", handle_bulk_mode_switch)
    app.router.add_post("/api/account/pause", handle_pause_account)
    app.router.add_post("/api/account/pause_till_4am", handle_pause_till_4am)
    app.router.add_post("/api/account/resume_now", handle_resume_now)
    app.router.add_post("/api/account/pause_all", handle_pause_all)
    app.router.add_post("/api/logs/clear", handle_clear_logs)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    asyncio.create_task(auto_resume_task())

    print(f"\033[92m[+] Dashboard: http://localhost:{port}\033[0m")
    print(f"\033[93m[+] Admin key: {ADMIN_KEY}\033[0m")
    print(f"\033[93m[+] User key : {USER_KEY}\033[0m")
    print(f"\033[93m[+] Telegram : {TELEGRAM_LINK}\033[0m")
    print(f"\033[93m[+] Auto LW  : Lv{AUTO_LW_LEVEL}+\033[0m")