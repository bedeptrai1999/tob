import os, time, sqlite3, logging, asyncio, io, random, math
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
DB_PATH = os.getenv("DB_PATH", "data/nuoi_heo.sqlite3")
if not TOKEN:
    raise RuntimeError("Thiếu DISCORD_TOKEN. Hãy cài biến môi trường trên hosting.")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nuoi-heo")

START_COINS, FEED_COST, PIG_PRICE = 1000, 50, 400
PEN_BASE_CAPACITY, PEN_UPGRADE_COST, DAILY_REWARD = 3, 500, 250
DAILY_COOLDOWN = 86400
RARITIES = {
    "Thường": {"weight": 70, "mult": 1.0, "emoji": "🐷"},
    "Hiếm": {"weight": 22, "mult": 1.6, "emoji": "💎"},
    "Sử thi": {"weight": 7, "mult": 2.5, "emoji": "🌟"},
    "Huyền thoại": {"weight": 1, "mult": 5.0, "emoji": "👑"},
}
BREED_COST = 150
BREED_COOLDOWN = 6 * 3600
MISSION_REWARD = 120

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")
db.executescript("""
CREATE TABLE IF NOT EXISTS players (
 user_id TEXT PRIMARY KEY, coins INTEGER NOT NULL DEFAULT 1000,
 pen_level INTEGER NOT NULL DEFAULT 1, last_daily INTEGER NOT NULL DEFAULT 0,
 created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pigs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL,
 name TEXT NOT NULL DEFAULT 'Heo Hồng', weight REAL NOT NULL DEFAULT 2.0,
 hunger INTEGER NOT NULL DEFAULT 80, health INTEGER NOT NULL DEFAULT 100,
 born_at INTEGER NOT NULL, last_update INTEGER NOT NULL,
 FOREIGN KEY(owner_id) REFERENCES players(user_id)
);
CREATE INDEX IF NOT EXISTS idx_pigs_owner ON pigs(owner_id);
CREATE TABLE IF NOT EXISTS mission_progress (
 user_id TEXT NOT NULL, mission_key TEXT NOT NULL, day_key TEXT NOT NULL,
 progress INTEGER NOT NULL DEFAULT 0, claimed INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(user_id, mission_key, day_key)
);
""")
# Safe schema migration for users upgrading from the previous bot versions.
existing_cols = {r["name"] for r in db.execute("PRAGMA table_info(pigs)").fetchall()}
for col, ddl in [
    ("rarity", "TEXT NOT NULL DEFAULT 'Thường'"),
    ("gender", "TEXT NOT NULL DEFAULT 'Đực'"),
    ("last_breed", "INTEGER NOT NULL DEFAULT 0"),
]:
    if col not in existing_cols:
        db.execute(f"ALTER TABLE pigs ADD COLUMN {col} {ddl}")
db.commit()

def now_ts(): return int(time.time())
def day_key(): return datetime.now(timezone.utc).strftime("%Y-%m-%d")
def capacity(player): return PEN_BASE_CAPACITY + (player["pen_level"] - 1) * 2
def fmt_time(seconds):
    seconds = max(0, seconds)
    return f"{seconds // 3600} giờ {(seconds % 3600) // 60} phút"
def weekly_event_index():
    return int(time.time() // 604800) % 3

def weekly_event():
    events = [
        ("🌽 Lễ hội thức ăn", "Cho heo ăn nhận thêm 20 xu mỗi lần."),
        ("💎 Tuần heo hiếm", "Cơ hội ra heo Hiếm/Sử thi/Huyền thoại tăng."),
        ("🐷 Hội chợ trang trại", "Giá bán heo tăng 15%."),
    ]
    return events[weekly_event_index()]

def rarity_roll():
    if weekly_event_index() == 1:
        return random.choices(list(RARITIES), weights=[55, 30, 12, 3], k=1)[0]
    return random.choices(list(RARITIES), weights=[v["weight"] for v in RARITIES.values()], k=1)[0]

def gender_roll(): return random.choice(["Đực", "Cái"])

def ensure_player(user_id):
    uid = str(user_id)
    player = db.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
    if player is None:
        now = now_ts()
        db.execute("INSERT INTO players(user_id,coins,pen_level,last_daily,created_at) VALUES(?,?,?,?,?)",
                   (uid, START_COINS, 1, 0, now))
        db.execute("""INSERT INTO pigs(owner_id,name,weight,hunger,health,born_at,last_update,rarity,gender)
                      VALUES(?,?,?,?,?,?,?,?,?)""",
                   (uid, "Heo Hồng", 2.0, 80, 100, now, now, "Thường", gender_roll()))
        db.commit()
    return db.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()

growth_events = {}

def update_pig(pig):
    old_stage = pig_stage(float(pig["weight"]))
    now = now_ts()
    elapsed = max(0, min((now - pig["last_update"]) / 3600, 24 * 30))
    hunger = max(0, int(pig["hunger"]) - int(elapsed * 4))
    growth = min(elapsed * 0.15, 5) if hunger >= 20 and pig["health"] > 0 else 0
    health = int(pig["health"])
    if hunger <= 0 and elapsed >= 1: health = max(10, health - int(elapsed * 2))
    db.execute("UPDATE pigs SET weight=?,hunger=?,health=?,last_update=? WHERE id=?",
               (round(float(pig["weight"]) + growth, 2), hunger, health, now, pig["id"]))
    db.commit()
    updated = db.execute("SELECT * FROM pigs WHERE id=?", (pig["id"],)).fetchone()
    new_stage = pig_stage(float(updated["weight"]))
    if new_stage != old_stage:
        growth_events[str(pig["owner_id"])] = (int(pig["id"]), new_stage)
    return updated

def get_farm(user_id):
    player = ensure_player(user_id)
    raw = db.execute("SELECT * FROM pigs WHERE owner_id=? ORDER BY id", (str(user_id),)).fetchall()
    pigs = [update_pig(p) for p in raw]
    player = db.execute("SELECT * FROM players WHERE user_id=?", (str(user_id),)).fetchone()
    return player, pigs

def bar(value, width=10):
    value = max(0, min(100, int(value)))
    filled = round(value / 100 * width)
    return "🟩" * filled + "⬜" * (width-filled) + f" **{value}%**"

def load_font(size=24, bold=False):
    candidates = [
      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
      "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
    ]
    for p in candidates:
        try: return ImageFont.truetype(p, size)
        except OSError: pass
    return ImageFont.load_default()

RARITY_PALETTES = {
    "Thường": ((255, 166, 190), (246, 112, 151), (255, 218, 228)),
    "Hiếm": ((135, 205, 255), (61, 143, 225), (220, 245, 255)),
    "Sử thi": ((202, 133, 255), (133, 69, 211), (250, 219, 255)),
    "Huyền thoại": ((255, 205, 73), (220, 133, 20), (255, 246, 174)),
}

def pig_stage(weight):
    if weight < 5: return "Heo con"
    if weight < 20: return "Thiếu niên"
    return "Trưởng thành"

def pig_colors(pig):
    base, snout, shine = RARITY_PALETTES.get(pig["rarity"], RARITY_PALETTES["Thường"])
    # Stable variation makes each pig look like an individual instead of a copy.
    rng = random.Random(int(pig["id"]) * 7919)
    shift = rng.randint(-12, 12)
    base = tuple(max(0, min(255, c + shift)) for c in base)
    return base, snout, shine, rng

def draw_pig(draw, cx, cy, scale, pig, frame=0, action="idle", facing=1):
    base, snout, shine, rng = pig_colors(pig)
    outline = (103, 65, 82)
    bob = int(math.sin(frame * math.pi / 2) * 3) if action in ("idle", "feed", "grow") else 0
    cx += 0
    cy += bob
    def box(x1,y1,x2,y2): return (int(x1),int(y1),int(x2),int(y2))
    # soft rarity aura and sparkles
    if pig["rarity"] != "Thường":
        aura = RARITY_PALETTES[pig["rarity"]][2]
        r = int(scale * (0.78 + 0.04 * (frame % 3)))
        draw.ellipse(box(cx-r,cy-r*0.72,cx+r,cy+r*0.78), fill=tuple(aura)+(0,) if False else aura, outline=aura, width=max(1,int(scale*.025)))
        for sx,sy in [(-.78,-.5),(.72,-.6),(.86,.28),(-.65,.45)]:
            x,y=cx+sx*scale,cy+sy*scale
            if (frame + int(abs(sx*10)))%3 != 1:
                draw.line(box(x-3*scale/30,y,x+3*scale/30,y),fill=(255,255,235),width=max(1,int(scale*.025)))
                draw.line(box(x,y-3*scale/30,x,y+3*scale/30),fill=(255,255,235),width=max(1,int(scale*.025)))
    # feet and rounded body
    legw=max(3,int(scale*.12)); leg_h=scale*.22
    for lx in (-.42,.22):
        x=cx+lx*scale
        draw.rounded_rectangle(box(x-legw/2,cy+scale*.35,x+legw/2,cy+scale*.35+leg_h),radius=max(2,int(scale*.04)),fill=base,outline=outline,width=max(1,int(scale*.025)))
    draw.ellipse(box(cx-scale*.62,cy-scale*.08,cx+scale*.52,cy+scale*.55),fill=base,outline=outline,width=max(1,int(scale*.035)))
    # unique patches, based on pig ID
    patch_rng=random.Random(int(pig["id"])*31)
    if patch_rng.random() > .35:
        px=cx+patch_rng.choice([-.3,.05,.24])*scale
        py=cy+patch_rng.choice([.03,.16])*scale
        draw.ellipse(box(px-scale*.12,py-scale*.08,px+scale*.12,py+scale*.08),fill=shine)
    # ears
    draw.ellipse(box(cx-scale*.48,cy-scale*.56,cx-scale*.12,cy-scale*.16),fill=base,outline=outline,width=max(1,int(scale*.03)))
    draw.ellipse(box(cx+scale*.12,cy-scale*.57,cx+scale*.47,cy-scale*.16),fill=base,outline=outline,width=max(1,int(scale*.03)))
    draw.ellipse(box(cx-scale*.40,cy-scale*.49,cx-scale*.20,cy-scale*.25),fill=snout)
    draw.ellipse(box(cx+scale*.20,cy-scale*.49,cx+scale*.39,cy-scale*.25),fill=snout)
    # head and cheeks
    draw.ellipse(box(cx-scale*.52,cy-scale*.43,cx+scale*.50,cy+scale*.39),fill=base,outline=outline,width=max(1,int(scale*.035)))
    draw.ellipse(box(cx-scale*.48,cy+scale*.06,cx-scale*.27,cy+scale*.20),fill=(255,133,164))
    draw.ellipse(box(cx+scale*.25,cy+scale*.06,cx+scale*.46,cy+scale*.20),fill=(255,133,164))
    # eyes vary by ID
    eye_y=cy-scale*.10
    eye_r=max(2,scale*.045)
    for ex in (-.20,.20):
        exx=cx+ex*scale
        draw.ellipse(box(exx-eye_r,eye_y-eye_r,exx+eye_r,eye_y+eye_r*1.4),fill=(48,40,52))
        draw.ellipse(box(exx-eye_r*.25,eye_y-eye_r*.65,exx+eye_r*.35,eye_y-eye_r*.05),fill=(255,255,255))
    draw.ellipse(box(cx-scale*.23,cy+scale*.02,cx+scale*.23,cy+scale*.25),fill=snout,outline=outline,width=max(1,int(scale*.025)))
    for nx in (-.09,.09): draw.ellipse(box(cx+nx*scale-scale*.025,cy+scale*.09,cx+nx*scale+scale*.025,cy+scale*.16),fill=(105,54,74))
    # little crown/flower/bandana accessories per rarity
    if pig["rarity"] == "Huyền thoại":
        draw.polygon([(cx-scale*.25,cy-scale*.42),(cx-scale*.18,cy-scale*.68),(cx-scale*.04,cy-scale*.49),(cx+scale*.08,cy-scale*.70),(cx+scale*.20,cy-scale*.42)],fill=(255,220,74),outline=(156,101,22))
    elif pig["rarity"] == "Sử thi":
        draw.ellipse(box(cx+scale*.18,cy-scale*.60,cx+scale*.38,cy-scale*.40),fill=(255,112,198),outline=(129,59,160))
        draw.ellipse(box(cx+scale*.23,cy-scale*.55,cx+scale*.33,cy-scale*.45),fill=(255,231,120))
    elif pig["rarity"] == "Hiếm":
        draw.polygon([(cx-scale*.26,cy-scale*.42),(cx-scale*.15,cy-scale*.55),(cx-scale*.04,cy-scale*.42)],fill=(117,236,255),outline=(49,121,194))
    # action props
    if action == "feed":
        draw.ellipse(box(cx+scale*.42,cy+scale*.20,cx+scale*.86,cy+scale*.40),fill=(188,120,67),outline=(105,66,45),width=max(1,int(scale*.025)))
        for k in range(4):
            xx=cx+scale*(.48+k*.08); yy=cy+scale*(.24+(k%2)*.04)
            draw.ellipse(box(xx,yy,xx+scale*.055,yy+scale*.06),fill=(255,210,73),outline=(163,111,36))
        draw.text((cx-scale*.08,cy-scale*.95),"♥",font=load_font(max(12,int(scale*.22)),True),fill=(255,79,127),anchor="mm")
    elif action == "breed":
        draw.text((cx,cy-scale*.80),"♥",font=load_font(max(15,int(scale*.28)),True),fill=(255,76,140),anchor="mm")
    elif action == "grow":
        draw.text((cx+scale*.55,cy-scale*.55),"✦",font=load_font(max(12,int(scale*.2)),True),fill=(255,239,116),anchor="mm")
    elif action == "sleep":
        draw.text((cx+scale*.50,cy-scale*.50),"Zz",font=load_font(max(12,int(scale*.19)),True),fill=(112,168,238),anchor="mm")

def farm_art(player, pigs):
    im=Image.new("RGB",(1000,390),(166,220,248)); d=ImageDraw.Draw(im)
    d.ellipse((820,25,900,105),fill=(255,221,105))
    for x,y in [(90,50),(370,28),(680,65)]:
        d.ellipse((x,y+15,x+82,y+52),fill=(255,255,255)); d.ellipse((x+22,y,x+100,y+52),fill=(255,255,255))
    d.rectangle((0,205,1000,390),fill=(111,188,94))
    d.ellipse((-100,155,470,350),fill=(136,203,111)); d.ellipse((420,160,1100,390),fill=(102,176,87))
    # barn
    d.rectangle((45,135,245,285),fill=(192,75,73),outline=(119,49,49),width=5)
    d.polygon([(20,140),(145,52),(270,140)],fill=(126,53,52),outline=(91,40,40))
    d.rectangle((111,200,177,285),fill=(243,216,164),outline=(119,49,49),width=4)
    d.line((144,200,144,283),fill=(119,49,49),width=4)
    # fence
    for x in range(280,960,43): d.rectangle((x,232,x+10,315),fill=(230,194,137),outline=(145,105,64),width=2)
    d.rectangle((275,246,970,259),fill=(230,194,137),outline=(145,105,64),width=2)
    d.rectangle((275,285,970,298),fill=(230,194,137),outline=(145,105,64),width=2)
    positions=[(365,185),(520,210),(680,177),(835,220)]
    for idx,pig in enumerate(pigs[:4]):
        x,y=positions[idx]
        # Each pig has its own colors, face markings and rarity accessories.
        draw_pig(d,x,y,72,pig,frame=int(pig["id"])%4)
        d.rounded_rectangle((x-65,y+66,x+65,y+91),radius=8,fill=(255,255,255),outline=(225,225,225),width=1)
        d.text((x,y+70),f"#{pig['id']} {pig['name'][:10]}",font=load_font(12,True),fill=(62,68,78),anchor="ma")
    d.rounded_rectangle((18,12,570,55),radius=13,fill=(255,255,255),outline=(226,226,226),width=2)
    d.text((32,19),f"TRANG TRAI NUOI HEO  |  CHUONG CAP {player['pen_level']}",font=load_font(20,True),fill=(69,75,83))
    d.text((24,354),f"{len(pigs)} heo  |  {player['coins']:,} xu  |  Mỗi heo có ngoại hình riêng",font=load_font(20,True),fill=(38,78,40))
    out=io.BytesIO(); im.save(out,format="PNG"); out.seek(0); return out

def pig_action_gif(pig, action="feed", mate=None, child=None):
    """Create a tiny animated action scene at runtime; no external art files required."""
    frames=[]
    for frame in range(6):
        im=Image.new("RGBA",(520,300),(30,42,68,255)); d=ImageDraw.Draw(im)
        # dreamy backdrop
        d.ellipse((20,20,500,300),fill=(43,62,91,255))
        for k in range(16):
            x=(k*37+frame*7)%510; y=(k*53+frame*11)%270
            d.ellipse((x,y,x+3,y+3),fill=(255,238,150,255))
        title={"feed":"GIỜ ĂN VUI VẺ","breed":"HEO CON CHÀO ĐỜI","grow":"LỚN LÊN RỒI!","sleep":"NGỦ NGON","buy":"HEO CON MỚI"}.get(action,"CHĂM SÓC HEO")
        d.text((260,22),title,font=load_font(22,True),fill=(255,255,255),anchor="mt",stroke_width=1,stroke_fill=(33,39,56))
        if action == "breed" and mate:
            draw_pig(d,165,155,88,pig,frame,"breed")
            draw_pig(d,350,155,88,mate,frame,"breed")
            # baby appears and grows slightly during the animation
            baby = dict(child or pig)
            draw_pig(d,260,225,34,baby,frame,"grow")
            d.text((260,278),"💕  +1 HEo CON  💕",font=load_font(16,True),fill=(255,180,211),anchor="mm")
        elif action == "grow":
            stage_scale=[48,62,80,90,98,105][frame]
            draw_pig(d,260,165,stage_scale,pig,frame,"grow")
            d.text((260,270),["HEO CON","LỚN NHANH","THIẾU NIÊN","KHỎE MẠNH","SẮP TRƯỞNG THÀNH","TRƯỞNG THÀNH!"][frame],font=load_font(18,True),fill=(255,239,155),anchor="mm")
        else:
            draw_pig(d,260,160,100,pig,frame,action)
            if action == "feed":
                d.text((260,270),["NHAM NHAM!","NGON QUÁ!","ĂN THÊM NÀO!","♥ +35 ĐỘ NO ♥","BỤNG NO CĂNG!","CẢM ƠN CHỦ! 🐷"][frame],font=load_font(17,True),fill=(255,225,146),anchor="mm")
            elif action == "sleep":
                d.text((260,270),"Z z z ...",font=load_font(20,True),fill=(176,215,255),anchor="mm")
            else:
                d.text((260,270),f"{pig['name']} · {pig['rarity']}",font=load_font(17,True),fill=(255,255,255),anchor="mm")
        frames.append(im.convert("P",palette=Image.Palette.ADAPTIVE))
    out=io.BytesIO(); frames[0].save(out,format="GIF",save_all=True,append_images=frames[1:],duration=170,loop=0,disposal=2); out.seek(0); return out

def farm_embed(user, notice="", selected_id=None):
    player,pigs=get_farm(user.id)
    selected=next((p for p in pigs if p["id"]==selected_id),None)
    if selected is None and pigs: selected=pigs[0]
    desc=(notice+"\n\n" if notice else "")+"Chọn heo trong menu rồi bấm nút để chăm sóc, bán hoặc phối giống."
    e=discord.Embed(title=f"🐷 NÔNG TRẠI CỦA {user.display_name.upper()}",description=desc,color=discord.Color.from_rgb(242,145,166))
    if selected:
        r=RARITIES.get(selected["rarity"],RARITIES["Thường"])
        e.add_field(name=f"{r['emoji']} {selected['name']} · #{selected['id']} · {selected['rarity']}",
          value=f"⚥ **Giới tính:** {selected['gender']} · ⚖️ **Cân nặng:** {selected['weight']:.1f} kg\n🍚 **Độ no:** {bar(selected['hunger'])}\n❤️ **Sức khỏe:** {bar(selected['health'])}",inline=False)
    e.add_field(name="💰 Ví xu",value=f"**{player['coins']:,}** xu",inline=True)
    e.add_field(name="🏠 Chuồng",value=f"Cấp **{player['pen_level']}**",inline=True)
    e.add_field(name="🐷 Sức chứa",value=f"**{len(pigs)}/{capacity(player)}**",inline=True)
    e.set_image(url="attachment://farm.png")
    e.set_footer(text="Heo hiếm bán đắt hơn. Chăm sóc tốt để tăng giá trị.")
    return e

def mission_state(user_id, key):
    day=day_key()
    row=db.execute("SELECT * FROM mission_progress WHERE user_id=? AND mission_key=? AND day_key=?",
                   (str(user_id),key,day)).fetchone()
    if not row:
        db.execute("INSERT OR IGNORE INTO mission_progress(user_id,mission_key,day_key,progress,claimed) VALUES(?,?,?,?,?)",
                   (str(user_id),key,day,0,0)); db.commit()
        row=db.execute("SELECT * FROM mission_progress WHERE user_id=? AND mission_key=? AND day_key=?",
                       (str(user_id),key,day)).fetchone()
    return row

def add_progress(user_id,key,amount=1):
    row=mission_state(user_id,key)
    if not row["claimed"]:
        db.execute("UPDATE mission_progress SET progress=MIN(?,progress+?) WHERE user_id=? AND mission_key=? AND day_key=?",
                   (3,amount,str(user_id),key,day_key()))
        db.commit()

MISSIONS={
 "feed":("Cho heo ăn", "Cho heo ăn 3 lần", 3),
 "buy":("Nhà sưu tập", "Mua heo mới 1 lần", 1),
 "breed":("Chủ trại mát tay", "Sinh sản 1 lần", 1),
}

class PigSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="🐷 Chọn heo muốn chăm sóc...",min_values=1,max_values=1,custom_id="farm:pig_select",row=0)
    async def callback(self,interaction):
        view=self.view
        chosen=int(self.values[0])
        if chosen==0:
            await view.render(interaction,"Chuồng đang trống. Bấm Mua heo để bắt đầu."); return
        view.selected_pigs[interaction.user.id]=chosen
        await view.render(interaction,f"Đã chọn heo #{chosen}.")

class FarmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.selected_pigs={}
        self.add_item(PigSelect())

    def sync_select(self,user_id):
        _,pigs=get_farm(user_id)
        select=next((x for x in self.children if isinstance(x,PigSelect)),None)
        if not select: return
        current=self.selected_pigs.get(user_id)
        options=[discord.SelectOption(label=f"#{p['id']} · {p['name'][:55]}",
          description=f"{p['rarity']} · {p['gender']} · {p['weight']:.1f} kg",
          value=str(p["id"]),default=p["id"]==current) for p in pigs[:25]]
        if not options: options=[discord.SelectOption(label="Chuồng đang trống",value="0",description="Bấm Mua heo để bắt đầu")]
        select.options=options
        if pigs and current not in [p["id"] for p in pigs]: self.selected_pigs[user_id]=pigs[0]["id"]
        if not pigs: self.selected_pigs[user_id]=None

    async def render(self,interaction,notice="",animate=False,action=None,action_pig=None,action_mate=None,action_child=None):
        uid=str(interaction.user.id)
        self.sync_select(interaction.user.id)
        growth = growth_events.pop(uid, None)
        if growth:
            action="grow"
            action_pig=next((p for p in get_farm(interaction.user.id)[1] if p["id"]==growth[0]),None)
            action_mate=None
            action_child=None
            if action_pig:
                notice += f" ✨ {action_pig['name']} đã phát triển thành giai đoạn **{growth[1]}**!"
        if animate:
            await interaction.response.defer()
            await asyncio.sleep(0.25)
            notice="✨ "+notice
        e=farm_embed(interaction.user,notice,self.selected_pigs.get(interaction.user.id))
        player,pigs=get_farm(interaction.user.id)
        farm=farm_art(player,pigs)
        files=[discord.File(farm,filename="farm.png")]
        if action and action_pig:
            action_gif=pig_action_gif(action_pig,action,action_mate,action_child)
            files.append(discord.File(action_gif,filename="pig_action.gif"))
            e.set_image(url="attachment://pig_action.gif")
            e.set_thumbnail(url="attachment://farm.png")
        if animate:
            await interaction.edit_original_response(embed=e,attachments=files,view=self)
        else:
            await interaction.response.edit_message(embed=e,attachments=files,view=self)

    @discord.ui.button(label="Trang trại",emoji="🏡",style=discord.ButtonStyle.primary,custom_id="farm:home",row=1)
    async def home(self,i,b): await self.render(i,"Đã làm mới trang trại.")

    @discord.ui.button(label="Cho ăn",emoji="🌽",style=discord.ButtonStyle.success,custom_id="farm:feed",row=1)
    async def feed(self,i,b):
        player,pigs=get_farm(i.user.id); pid=self.selected_pigs.get(i.user.id)
        pig=next((p for p in pigs if p["id"]==pid),None)
        if not pig: await self.render(i,"Chọn heo trước khi cho ăn."); return
        if player["coins"]<FEED_COST: await self.render(i,f"Không đủ xu. Thức ăn giá {FEED_COST} xu."); return
        if pig["hunger"]>=100: await self.render(i,f"{pig['name']} đã no căng rồi!"); return
        hunger=min(100,pig["hunger"]+35); health=min(100,pig["health"]+2)
        bonus = 20 if weekly_event_index() == 0 else 0
        db.execute("UPDATE players SET coins=coins-?+? WHERE user_id=?",(FEED_COST,bonus,str(i.user.id)))
        db.execute("UPDATE pigs SET hunger=?,health=? WHERE id=? AND owner_id=?",(hunger,health,pig["id"],str(i.user.id))); db.commit()
        add_progress(i.user.id,"feed")
        extra = f" Sự kiện tặng thêm {bonus} xu!" if bonus else ""
        await self.render(i,f"{pig['name']} ăn ngon lành! Độ no {hunger}%, trừ {FEED_COST} xu.{extra}",True,action="feed",action_pig=pig)

    @discord.ui.button(label="Điểm danh",emoji="🎁",style=discord.ButtonStyle.success,custom_id="farm:daily",row=1)
    async def daily(self,i,b):
        p=ensure_player(i.user.id); now=now_ts(); rem=DAILY_COOLDOWN-(now-p["last_daily"])
        if rem>0: await self.render(i,f"Đã nhận quà rồi. Quay lại sau {fmt_time(rem)}."); return
        db.execute("UPDATE players SET coins=coins+?,last_daily=? WHERE user_id=?",(DAILY_REWARD,now,str(i.user.id))); db.commit()
        await self.render(i,f"Nhận thành công {DAILY_REWARD} xu!",True)

    @discord.ui.button(label="Mua heo",emoji="🐖",style=discord.ButtonStyle.primary,custom_id="farm:buy",row=2)
    async def buy(self,i,b):
        p,pigs=get_farm(i.user.id)
        if len(pigs)>=capacity(p): await self.render(i,"Chuồng đầy. Nâng cấp chuồng trước."); return
        if p["coins"]<PIG_PRICE: await self.render(i,f"Cần {PIG_PRICE} xu để mua heo con."); return
        now=now_ts(); rarity=rarity_roll(); gender=gender_roll()
        cur=db.execute("UPDATE players SET coins=coins-? WHERE user_id=?",(PIG_PRICE,str(i.user.id)))
        cur=db.execute("""INSERT INTO pigs(owner_id,name,weight,hunger,health,born_at,last_update,rarity,gender)
                          VALUES(?,?,?,?,?,?,?,?,?)""",(str(i.user.id),f"Heo {rarity}",2.0,80,100,now,now,rarity,gender))
        db.commit(); self.selected_pigs[i.user.id]=cur.lastrowid; add_progress(i.user.id,"buy")
        r=RARITIES[rarity]
        newpig=db.execute("SELECT * FROM pigs WHERE id=?",(cur.lastrowid,)).fetchone()
        await self.render(i,f"{r['emoji']} Mua được **{rarity}** ({gender})! Heo mới #{cur.lastrowid}.",True,action="buy",action_pig=newpig)

    @discord.ui.button(label="Nâng chuồng",emoji="🏠",style=discord.ButtonStyle.secondary,custom_id="farm:upgrade",row=2)
    async def upgrade(self,i,b):
        p=ensure_player(i.user.id); cost=PEN_UPGRADE_COST*p["pen_level"]
        if p["coins"]<cost: await self.render(i,f"Cần {cost:,} xu để nâng chuồng."); return
        db.execute("UPDATE players SET coins=coins-?,pen_level=pen_level+1 WHERE user_id=?",(cost,str(i.user.id))); db.commit()
        p=ensure_player(i.user.id)
        await self.render(i,f"Nâng chuồng thành công! Cấp {p['pen_level']}, sức chứa {capacity(p)} heo.",True)

    @discord.ui.button(label="Bán heo đã chọn",emoji="💰",style=discord.ButtonStyle.danger,custom_id="farm:sell",row=2)
    async def sell(self,i,b):
        p,pigs=get_farm(i.user.id); pid=self.selected_pigs.get(i.user.id)
        pig=next((x for x in pigs if x["id"]==pid),None)
        if not pig: await self.render(i,"Chọn con heo muốn bán."); return
        if len(pigs)<=1: await self.render(i,"Phải giữ lại ít nhất một con heo."); return
        price=max(50,int((180+pig["weight"]*35+pig["health"]*1.5)*RARITIES.get(pig["rarity"],RARITIES["Thường"])["mult"]))
        if weekly_event_index() == 2:
            price = int(price * 1.15)
        db.execute("DELETE FROM pigs WHERE id=? AND owner_id=?",(pig["id"],str(i.user.id)))
        db.execute("UPDATE players SET coins=coins+? WHERE user_id=?",(price,str(i.user.id))); db.commit()
        await self.render(i,f"Đã bán {pig['name']} với giá **{price:,} xu**.",True)

    @discord.ui.button(label="Sinh sản",emoji="💕",style=discord.ButtonStyle.primary,custom_id="farm:breed",row=3)
    async def breed(self,i,b):
        p,pigs=get_farm(i.user.id); pid=self.selected_pigs.get(i.user.id)
        pig=next((x for x in pigs if x["id"]==pid),None)
        if not pig: await self.render(i,"Chọn một heo trước."); return
        if pig["last_breed"] and now_ts()-pig["last_breed"]<BREED_COOLDOWN:
            await self.render(i,f"Heo này cần nghỉ thêm {fmt_time(BREED_COOLDOWN-(now_ts()-pig['last_breed']))}."); return
        mate=next((x for x in pigs if x["id"]!=pig["id"] and x["gender"]!=pig["gender"] and (not x["last_breed"] or now_ts()-x["last_breed"]>=BREED_COOLDOWN)),None)
        if not mate: await self.render(i,"Cần ít nhất một heo khác giới tính và đã hết thời gian nghỉ."); return
        if len(pigs)>=capacity(p): await self.render(i,"Chuồng đầy, hãy nâng chuồng trước khi sinh sản."); return
        if p["coins"]<BREED_COST: await self.render(i,f"Phối giống tốn {BREED_COST} xu."); return
        # Offspring inherits the better rarity occasionally; otherwise a weighted rarity roll.
        rarity= pig["rarity"] if random.random()<0.18 else (mate["rarity"] if random.random()<0.18 else rarity_roll())
        gender=gender_roll(); now=now_ts()
        db.execute("UPDATE players SET coins=coins-? WHERE user_id=?",(BREED_COST,str(i.user.id)))
        db.execute("UPDATE pigs SET last_breed=? WHERE id IN (?,?)",(now,pig["id"],mate["id"]))
        cur=db.execute("""INSERT INTO pigs(owner_id,name,weight,hunger,health,born_at,last_update,rarity,gender)
                          VALUES(?,?,?,?,?,?,?,?,?)""",(str(i.user.id),f"Heo con {rarity}",1.0,90,100,now,now,rarity,gender))
        db.commit(); self.selected_pigs[i.user.id]=cur.lastrowid; add_progress(i.user.id,"breed")
        newpig=db.execute("SELECT * FROM pigs WHERE id=?",(cur.lastrowid,)).fetchone()
        await self.render(i,f"💕 {pig['name']} và {mate['name']} sinh ra **heo con {rarity}** ({gender})! Tốn {BREED_COST} xu.",True,action="breed",action_pig=pig,action_mate=mate,action_child=newpig)

    @discord.ui.button(label="Nhiệm vụ",emoji="📜",style=discord.ButtonStyle.secondary,custom_id="farm:missions",row=3)
    async def missions(self,i,b):
        ensure_player(i.user.id)
        lines=[]
        for key,(title,desc,target) in MISSIONS.items():
            row=mission_state(i.user.id,key)
            status="✅ Đã nhận" if row["claimed"] else ("🎁 Có thể nhận" if row["progress"]>=target else "Đang làm")
            lines.append(f"**{title}** — {desc}\nTiến độ: **{min(row['progress'],target)}/{target}** · {status}")
        e=discord.Embed(title="📜 NHIỆM VỤ HẰNG NGÀY",description="\n\n".join(lines),color=discord.Color.blurple())
        e.set_footer(text=f"Nhận thưởng: {MISSION_REWARD} xu mỗi nhiệm vụ hoàn thành.")
        await i.response.send_message(embed=e,view=MissionView(),ephemeral=True)

    @discord.ui.button(label="Sự kiện",emoji="🎉",style=discord.ButtonStyle.secondary,custom_id="farm:event",row=3)
    async def event(self,i,b):
        title,desc=weekly_event()
        e=discord.Embed(title=f"🎉 SỰ KIỆN TUẦN: {title}",description=desc,color=discord.Color.orange())
        e.add_field(name="Thời gian",value="Luân phiên theo tuần UTC",inline=False)
        await i.response.send_message(embed=e,ephemeral=True)

class MissionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
    @discord.ui.button(label="Nhận thưởng nhiệm vụ",emoji="🎁",style=discord.ButtonStyle.success)
    async def claim(self,i,b):
        ensure_player(i.user.id); claimed=[]
        for key,(title,desc,target) in MISSIONS.items():
            row=mission_state(i.user.id,key)
            if row["progress"]>=target and not row["claimed"]:
                db.execute("UPDATE mission_progress SET claimed=1 WHERE user_id=? AND mission_key=? AND day_key=?",
                           (str(i.user.id),key,day_key()))
                db.execute("UPDATE players SET coins=coins+? WHERE user_id=?",(MISSION_REWARD,str(i.user.id)))
                claimed.append(title)
        db.commit()
        if claimed:
            await i.response.send_message(f"🎉 Đã nhận {MISSION_REWARD*len(claimed)} xu từ: {', '.join(claimed)}.",ephemeral=True)
        else:
            await i.response.send_message("Chưa có nhiệm vụ nào đủ điều kiện nhận thưởng.",ephemeral=True)

intents=discord.Intents.default()
bot=commands.Bot(command_prefix="!",intents=intents)

@bot.event
async def setup_hook():
    bot.add_view(FarmView())
    try:
        if GUILD_ID and GUILD_ID.isdigit():
            guild=discord.Object(id=int(GUILD_ID)); bot.tree.copy_global_to(guild=guild)
            synced=await bot.tree.sync(guild=guild)
        else: synced=await bot.tree.sync()
        log.info("Synced %s slash commands",len(synced))
    except Exception: log.exception("Slash command sync failed")

@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s)",bot.user,bot.user.id if bot.user else "?")

@bot.tree.command(name="farm",description="Mở bảng điều khiển Nuôi Heo bằng button")
async def farm(interaction:discord.Interaction):
    p,pigs=get_farm(interaction.user.id)
    view=FarmView(); view.selected_pigs[interaction.user.id]=pigs[0]["id"] if pigs else None
    view.sync_select(interaction.user.id)
    e=farm_embed(interaction.user,"Chào mừng đến trang trại! Bấm nút để chơi.",view.selected_pigs.get(interaction.user.id))
    await interaction.response.send_message(embed=e,view=view,file=discord.File(farm_art(p,pigs),filename="farm.png"))

@bot.tree.command(name="ping",description="Kiểm tra bot có đang hoạt động không")
async def ping(interaction:discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! {round(bot.latency*1000)} ms",ephemeral=True)

try: bot.run(TOKEN,log_handler=None)
finally: db.close()
