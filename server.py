import asyncio, hashlib, hmac, json, os, random
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web, WSMsgType
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, WebAppInfo
from telegram.ext import Application, CommandHandler

TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("WEBAPP_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
APP_NAME = os.environ.get("APP_SHORT_NAME", "play")   # BotFather /newapp dagi qisqa nom
NAMES = ["Qizil", "Yashil", "Sariq", "Ko'k"]
START = [0, 13, 26, 39]
SAFE = {0, 8, 13, 21, 26, 34, 39, 47}
TURN_LIMIT = 40  # soniya: o'yinchi javob bermasa, avtomatik yuriladi
rooms = {}


def auth(init, guest):
    """Telegram initData ni tekshiradi (soxta foydalanuvchidan himoya)."""
    if init:
        data = dict(parse_qsl(init))
        h = data.pop("hash", "")
        chk = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), h):
            u = json.loads(data["user"])
            return {"uid": str(u["id"]), "name": u.get("first_name", "O'yinchi")[:16]}
    return {"uid": "guest" + str(guest)[:12], "name": "Mehmon"}


class Room:
    def __init__(s, rid):
        s.id, s.clients, s.seats, s.gen = rid, {}, [None] * 4, 0
        s.reset()

    def reset(s):
        s.phase, s.pcs = "lobby", [[-1] * 4 for _ in range(4)]
        s.turn, s.roll, s.die, s.movable, s.msg, s.winner, s.busy = 0, None, 0, [], "", None, False

    def occupied(s):
        return [i for i in range(4) if s.seats[i]]

    def next_seat(s):
        o = s.occupied()
        return o[(o.index(s.turn) + 1) % len(o)]

    def view(s, uid):
        seats = [x and {"name": x["name"], "bot": x["bot"], "me": x["uid"] == uid} for x in s.seats]
        return dict(room=s.id, phase=s.phase, seats=seats, pcs=s.pcs, turn=s.turn, die=s.die,
                    roll=s.roll, movable=s.movable, msg=s.msg, winner=s.winner, busy=s.busy)

    async def push(s):
        s.gen += 1
        for ws, u in list(s.clients.items()):
            try:
                await ws.send_str(json.dumps(s.view(u["uid"])))
            except Exception:
                s.clients.pop(ws, None)

    def later(s, delay, fn):
        g = s.gen

        async def run():
            await asyncio.sleep(delay)
            if s.gen == g:
                await fn()
        asyncio.create_task(run())

    # ---------- o'yin oqimi ----------
    async def begin_turn(s, p):
        s.turn, s.roll, s.movable, s.busy = p, None, [], False
        await s.push()
        s.later(0.9 if s.seats[p]["bot"] else TURN_LIMIT, s.do_roll)

    async def do_roll(s):
        p = s.turn
        s.roll = s.die = random.randint(1, 6)
        s.movable = [i for i in range(4) if (s.roll == 6 if s.pcs[p][i] < 0 else s.pcs[p][i] + s.roll <= 56)]
        s.msg = ""
        if not s.movable:
            s.busy, s.msg = True, "Yurish yo'q"
            await s.push()
            return s.later(1.0, lambda: s.begin_turn(s.next_seat()))
        await s.push()
        if s.seats[p]["bot"]:
            s.later(0.9, lambda: s.move(s.bot_pick()))
        elif len(s.movable) == 1:
            s.later(0.5, lambda: s.move(s.movable[0]))
        else:
            s.later(TURN_LIMIT, lambda: s.move(s.bot_pick()))

    def bot_pick(s):
        p, best, best_sc = s.turn, s.movable[0], -1e9
        for i in s.movable:
            st = s.pcs[p][i]
            ns = 0 if st < 0 else st + s.roll
            sc = ns + (40 if st < 0 else 0) + (100 if ns == 56 else 30 if ns > 50 else 0)
            if ns <= 50:
                idx = (START[p] + ns) % 52
                if idx in SAFE:
                    sc += 10
                else:
                    for q in s.occupied():
                        if q != p:
                            sc += sum(60 + ns for t in s.pcs[q] if 0 <= t <= 50 and (START[q] + t) % 52 == idx)
            if sc > best_sc:
                best, best_sc = i, sc
        return best

    async def move(s, i):
        p, r = s.turn, s.roll
        extra, s.msg, s.busy = r == 6, "", True
        st = s.pcs[p][i]
        st = 0 if st < 0 else st + r
        s.pcs[p][i] = st
        if st <= 50:
            idx = (START[p] + st) % 52
            if idx not in SAFE:
                for q in s.occupied():
                    if q == p:
                        continue
                    for j in range(4):
                        t = s.pcs[q][j]
                        if 0 <= t <= 50 and (START[q] + t) % 52 == idx:
                            s.pcs[q][j], extra, s.msg = -1, True, f"{NAMES[q]} donasi urildi!"
        if st == 56:
            extra, s.msg = True, "Dona marraga yetdi!"
        s.roll, s.movable = None, []
        if all(v == 56 for v in s.pcs[p]):
            s.phase, s.winner = "done", p
            return await s.push()
        await s.push()
        s.later(0.7, lambda: s.begin_turn(p if extra else s.next_seat()))

    # ---------- foydalanuvchi harakatlari ----------
    async def act(s, u, a, d):
        uid = u["uid"]
        mine = [i for i, x in enumerate(s.seats) if x and x["uid"] == uid]
        seat = d.get("seat")
        if s.phase == "lobby":
            if a == "sit" and seat in range(4) and not s.seats[seat]:
                for m in mine:
                    s.seats[m] = None
                s.seats[seat] = {"uid": uid, "name": u["name"], "bot": False}
            elif a == "leave":
                for m in mine:
                    s.seats[m] = None
            elif a == "bot" and seat in range(4):
                if not s.seats[seat]:
                    s.seats[seat] = {"uid": None, "name": "Bot", "bot": True}
                elif s.seats[seat]["bot"]:
                    s.seats[seat] = None
            elif a == "start" and mine and len(s.occupied()) >= 2:
                s.phase, s.pcs = "play", [[-1] * 4 for _ in range(4)]
                return await s.begin_turn(s.occupied()[0])
            await s.push()
        elif s.phase == "play":
            cur = s.seats[s.turn]
            if not cur or cur["uid"] != uid or s.busy:
                return
            if a == "roll" and s.roll is None:
                await s.do_roll()
            elif a == "move" and s.roll is not None and d.get("piece") in s.movable:
                await s.move(d["piece"])
        elif s.phase == "done" and a == "again" and mine:
            s.reset()
            await s.push()


async def ws_handler(req):
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(req)
    room = user = None
    async for m in ws:
        if m.type != WSMsgType.TEXT:
            continue
        try:
            d = json.loads(m.data)
        except ValueError:
            continue
        if d.get("a") == "join":
            user = auth(d.get("init"), d.get("guest"))
            rid = str(d.get("room", "demo"))[:40]
            room = rooms.setdefault(rid, Room(rid))
            room.clients[ws] = user
            await room.push()
        elif room:
            await room.act(user, d.get("a"), d)
    if room:
        room.clients.pop(ws, None)
    return ws


async def index(_):
    return web.FileResponse(Path(__file__).parent / "index.html")


# ---------- Telegram bot ----------
async def start(update, ctx):
    chat = update.effective_chat
    if chat.type != "private":
        return await ludo(update, ctx)
    url = f"{BASE_URL}/?room=p{update.effective_user.id}"
    await update.message.reply_text(
        "Salom! 🎲 Meni guruhga qo'shing va guruhda /ludo deb yozing: hamma birga o'ynaydi.",
        reply_markup=Kb([[Btn("Botlarga qarshi mashq", web_app=WebAppInfo(url=url))]]))


async def ludo(update, ctx):
    chat = update.effective_chat
    if chat.type == "private":
        return await start(update, ctx)
    url = f"https://t.me/{ctx.bot.username}/{APP_NAME}?startapp=g{abs(chat.id)}"
    await update.message.reply_text("🎲 Ludo xonasi ochildi! Qo'shilish uchun bosing:",
                                    reply_markup=Kb([[Btn("▶️ O'ynash", url=url)]]))


async def main():
    bot = Application.builder().token(TOKEN).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CommandHandler("ludo", ludo))
    site = web.Application()
    site.add_routes([web.get("/", index), web.get("/ws", ws_handler)])
    runner = web.AppRunner(site)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080))).start()
    async with bot:
        await bot.start()
        await bot.updater.start_polling()
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
