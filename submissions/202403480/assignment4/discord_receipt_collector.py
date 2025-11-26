# app/discord_receipt_collector.py
import os, io, re, csv, uuid, time
from collections import deque
from datetime import datetime
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ui import View, Button, Modal, TextInput
from PIL import Image
import pytesseract
from dotenv import load_dotenv

# -------- 기본 셋업 --------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DATA_DIR = os.getenv("DATA_DIR", "./data")
TESSERACT_CMD = os.getenv("TESSERACT_CMD")  # 예: C:\\Program Files\\Tesseract-OCR\\tesseract.exe
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
CSV_PATH   = os.path.join(DATA_DIR, "labels.csv")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(CSV_PATH):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id","filename","category","amount","datetime","ocr_text_path"])

# --- vendor map loader (CSV: vendor,category,alias_regex) ---
VENDOR_MAP_PATH = os.path.join(DATA_DIR, "vendor_map.csv")
_vendor_patterns = []  # (compiled_regex, category, vendor)

if os.path.exists(VENDOR_MAP_PATH):
    import csv as _csv, re as _re
    with open(VENDOR_MAP_PATH, encoding="utf-8") as f:
        r = _csv.DictReader(f)
        for row in r:
            try:
                pat = _re.compile(row["alias_regex"], _re.I)
                _vendor_patterns.append((pat, row["category"], row["vendor"]))
            except Exception:
                # 잘못된 정규식은 건너뜀
                pass
    print(f"[vendor_map] loaded patterns:", len(_vendor_patterns))

intents = discord.Intents.default()
intents.message_content = True  # Bot 탭에서 Message Content Intent ON 필요
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# 최근 message 중복 방지(실수로 두 번 실행/재전송 등)
_recent_seen = {}
def seen_before(msg_id, ttl=10):
    now = time.time()
    # 오래된 것 정리
    for k in list(_recent_seen.keys()):
        if now - _recent_seen[k] > ttl:
            _recent_seen.pop(k, None)
    if msg_id in _recent_seen:
        return True
    _recent_seen[msg_id] = now
    return False

# -------- OCR/파싱 유틸 --------
def ocr_text(image_path: str) -> str:
    # kor+eng 을 우선 시도하고 실패하면 eng로
    for lang in ("kor+eng", "eng"):
        try:
            return pytesseract.image_to_string(Image.open(image_path), lang=lang, config="--oem 3 --psm 6")
        except Exception:
            continue
    return ""

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()

def guess_amount(text: str):
    # 1,234,567 / 1234567 / 12,345원 등
    candidates = []
    for m in re.finditer(r"(\d{1,3}(?:[,\s]\d{3})+|\d{4,})(?=\s*(?:원|krw|₩)?\b)", text, flags=re.IGNORECASE):
        val = int(re.sub(r"[^\d]", "", m.group(1)))
        candidates.append(val)
    # 너무 작은 값(예: 개수, 포인트 등) 거르기: 1000원 이상 우선
    big = [v for v in candidates if v >= 1000]
    if big:
        return max(big)
    return max(candidates) if candidates else None

def guess_datetime(text: str):
    # 날짜: 2025-11-13 / 2025.11.13 / 2025/11/13 / 25-11-13 ...
    date_patts = [
        r"(\d{4})[.\-\/](\d{1,2})[.\-\/](\d{1,2})",
        r"(\d{2})[.\-\/](\d{1,2})[.\-\/](\d{1,2})"
    ]
    time_patt = r"(\d{1,2}):(\d{2})(?::(\d{2}))?"

    yy, mm, dd = None, None, None
    hh, mi, ss = 0, 0, 0

    for dp in date_patts:
        m = re.search(dp, text)
        if m:
            a, b, c = m.groups()
            a, b, c = int(a), int(b), int(c)
            if len(m.group(1)) == 4:    # YYYY-MM-DD
                yy, mm, dd = a, b, c
            else:                       # YY-MM-DD
                yy, mm, dd = 2000 + a, b, c
            break

    m = re.search(time_patt, text)
    if m:
        hh, mi = int(m.group(1)), int(m.group(2))
        if m.group(3):
            ss = int(m.group(3))

    if yy and mm and dd:
        try:
            return datetime(yy, mm, dd, hh, mi, ss)
        except ValueError:
            pass
    return None

# 카테고리 룰(필요하면 아래 dict 확장)
CATEGORY_RULES = {
    "편의점":  ["gs25","cu","세븐일레븐","ministop","emart24","이마트24", "이마트에브리데이", "emarteveryday"],
    "카페":    ["스타벅스", "이디야커피", "투썸플레이스", "할리스", "메가MGC커피", "메가커피", "컴포즈커피", "빽다방", "더벤티", "폴바셋", "파스쿠찌", "드롭탑", "커피빈", "공차", "쥬씨", "달콤", "요거프레소", "더카페", "테라로사", "카페베네", "커피"],
    "식당":    ["식당","분식","치킨","피자","족발","냉면","칼국수","김밥","국밥","한솥","버거","맥도날드","롯데리아","버거킹"],
    "마트":    ["이마트","홈플러스","롯데마트","노브랜드","코스트코","마트","슈퍼"],
    "배달":    ["배달의민족","쿠팡이츠","요기요","딜리버리"],
    "교통":    ["버스","지하철","택시","코레일","철도","고속","교통","티머니"],
    "의료/약국":["약국","병원","의원","치과","의무","메디칼","처방"],
    "쇼핑":    ["무신사","쿠팡","네이버페이","11번가","지마켓","옥션","마켓컬리","ssg","위메프","티몬","롯데온"],
    "문화/여가":["영화","CGV","메가박스","롯데시네마","넷플릭스","뮤지컬","공연","노래방"],
}

def guess_category(text: str) -> str:
    t = normalize(text)

    # 1) vendor_map.csv 우선 매칭
    for pat, cat, _vendor in _vendor_patterns:
        if pat.search(t):
            return cat

    # 2) 폴백: CATEGORY_RULES 키워드 스코어링
    score = {}
    for cat, kws in CATEGORY_RULES.items():
        s = 0
        for kw in kws:
            if kw.lower() in t:
                s += 1
        if s > 0:
            score[cat] = s

    if score:
        return max(score.items(), key=lambda x: x[1])[0]
    return "기타"

# -------- 상호작용(확정/수정) --------
@dataclass
class Pending:
    id: str
    filename: str
    category: str
    amount: int|None
    when: datetime|None
    ocr_text: str

PENDING: dict[str, Pending] = {}

def save_row(p: Pending):
    text_path = os.path.join(DATA_DIR, f"{p.id}.txt")
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(p.ocr_text)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            p.id, p.filename, p.category,
            p.amount if p.amount is not None else "",
            p.when.strftime("%Y-%m-%d %H:%M:%S") if p.when else "",
            text_path
        ])

class CorrectionModal(Modal, title="값 수정"):
    def __init__(self, pid: str, init_cat: str, init_amt: str, init_dt: str):
        super().__init__(timeout=180)
        self.pid = pid
        self.t_category = TextInput(label="카테고리", default=init_cat, required=True, max_length=30)
        self.t_amount   = TextInput(label="금액(숫자)", default=init_amt, required=False, max_length=12)
        self.t_dt       = TextInput(label="일시(YYYY-MM-DD HH:MM:SS)", default=init_dt, required=False, max_length=25)
        self.add_item(self.t_category)
        self.add_item(self.t_amount)
        self.add_item(self.t_dt)

    async def on_submit(self, interaction: discord.Interaction):
        p = PENDING.get(self.pid)
        if not p:
            await interaction.response.send_message("세션을 찾지 못했습니다. 다시 시도해주세요.", ephemeral=True)
            return

        cat = self.t_category.value.strip() or "기타"
        amt = re.sub(r"[^\d]", "", self.t_amount.value or "")
        amt = int(amt) if amt else None
        dt  = self.t_dt.value.strip() if self.t_dt.value else ""
        when = None
        if dt:
            try:
                when = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
            except Exception:
                when = p.when

        p.category = cat
        p.amount   = amt if amt is not None else p.amount
        p.when     = when if when is not None else p.when

        save_row(p)
        PENDING.pop(self.pid, None)
        await interaction.response.send_message("✅ 수정값으로 저장했습니다.", ephemeral=False)

class ReviewView(View):
    def __init__(self, pid: str, cat: str, amt: int|None, when: datetime|None):
        super().__init__(timeout=180)
        self.pid = pid
        self.cat = cat
        self.amt = amt
        self.when = when

    @discord.ui.button(label="확정(저장)", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        p = PENDING.get(self.pid)
        if not p:
            await interaction.response.send_message("세션이 만료되었습니다. 다시 시도해주세요.", ephemeral=True)
            return
        save_row(p)
        PENDING.pop(self.pid, None)
        await interaction.response.send_message("✅ 저장했습니다.", ephemeral=False)
        self.disable_all_items()
        await interaction.message.edit(view=self)

    @discord.ui.button(label="수정", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: Button):
        default_dt = self.when.strftime("%Y-%m-%d %H:%M:%S") if self.when else ""
        default_amt = f"{self.amt}" if self.amt is not None else ""
        await interaction.response.send_modal(
            CorrectionModal(self.pid, self.cat, default_amt, default_dt)
        )

# -------- 핸들러 --------
@bot.event
async def on_ready():
    print(f"[READY] {bot.user} 로 로그인됨")

@bot.event
async def on_message(message: discord.Message):
    # DM만 처리 (원하면 서버 채널도 받게 바꿔줄 수 있음)
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return
    if seen_before(message.id):
        return

    if not message.attachments:
        return

    for att in message.attachments:
        if not att.content_type or not att.content_type.startswith("image/"):
            continue

        pid = uuid.uuid4().hex
        ext = os.path.splitext(att.filename)[1].lower() or ".jpg"
        fname = f"{pid}{ext}"
        save_path = os.path.join(UPLOAD_DIR, fname)
        data = await att.read()
        with open(save_path, "wb") as f:
            f.write(data)

        # OCR
        text = ocr_text(save_path)

        # 추정
        cat = guess_category(text)
        amt = guess_amount(text)
        when = guess_datetime(text)

        # Pending 세션
        PENDING[pid] = Pending(
            id=pid, filename=fname, category=cat, amount=amt, when=when, ocr_text=text
        )

        # 응답(임베드 + 버튼)
        emb = discord.Embed(title="영수증 자동 추정 결과", color=0x2ecc71)
        emb.add_field(name="카테고리(추정)", value=cat or "?", inline=False)
        emb.add_field(name="금액(추정)", value=f"{amt:,}원" if amt else "?", inline=True)
        emb.add_field(
            name="일시(추정)",
            value=when.strftime("%Y-%m-%d %H:%M:%S") if when else "?",
            inline=True
        )
        emb.set_footer(text="확정(저장) 또는 수정 버튼을 눌러주세요.")
        file_url = f"[파일보기]({att.url})"
        emb.add_field(name="원본", value=file_url, inline=False)

        view = ReviewView(pid, cat, amt, when)
        await message.channel.send(embed=emb, view=view)

# ---- 실행 ----
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN이 .env에 설정되어야 합니다.")
    bot.run(TOKEN)
