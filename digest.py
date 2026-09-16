#!/usr/bin/env python3
"""
Tech News Digest Bot – phiên bản Gemini 3.8 Flash
Nguồn cố định, lọc chặt, gửi Telegram 6:30 ICT mỗi ngày

Thay đổi so với bản cũ:
- Chuyển từ SDK legacy `google-generativeai` sang SDK mới `google-genai`
- Model chính: gemini-3.8-flash (free tier), thinking_level="low" cho tác vụ tóm tắt
- Tự fallback: gemini-3.8-flash → gemini-3.7-flash → gemini-2.5-flash nếu lỗi/429
"""

import os, re, time, feedparser, requests
from google import genai
from google.genai import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]

client = genai.Client(api_key=GEMINI_API_KEY)

ICT = ZoneInfo("Asia/Bangkok")

# ── Model: thử theo thứ tự, model nào chạy được thì dùng ─────────────────────
# Tất cả đều nằm trong free tier của Gemini API (ai.google.dev/gemini-api/docs/pricing)
MODEL_CHAIN = [
    "gemini-3.8-flash",   # mới nhất, thông minh nhất dòng Flash
    "gemini-3.7-flash",   # nhanh, ổn định
    "gemini-2.5-flash",   # fallback cuối – bản đang chạy trước đây
]

# ── Nguồn RSS cố định ────────────────────────────────────────────────────────
SOURCES = [
    {
        "name":  "Android Authority",
        "url":   "https://www.androidauthority.com/feed/",
        "count": 2,
        "focus": (
            "Android, smartphone, hệ sinh thái Google, phần mềm/dịch vụ liên quan. "
            "BỎ: khuyến mãi, deal, buyer's guide, review dài, how-to, opinion."
        ),
    },
    {
        "name":  "Windows Central",
        "url":   "https://www.windowscentral.com/feed",
        "count": 2,
        "focus": (
            "CHỈ hệ điều hành Windows (Windows 10/11/Server, Insider build, tính năng, cập nhật, policy). "
            "BỎ: phần cứng PC/laptop/Surface/GPU/CPU, game, Xbox, khuyến mãi."
        ),
    },
    {
        "name":  "PCWorld",
        "url":   "https://www.pcworld.com/index.rss",
        "count": 2,
        "focus": (
            "Phần cứng máy tính: CPU, GPU, RAM, SSD, mainboard, PSU, case, desktop/mini PC. "
            "Chỉ dạng news (announcement, launch, roadmap, benchmark, leak). "
            "BỎ: how-to, review, best-of, deal."
        ),
    },
    {
        "name":  "Wccftech",
        "url":   "https://wccftech.com/feed/",
        "count": 2,
        "focus": (
            "Phần cứng: CPU, GPU, RAM, motherboard, kiến trúc mới, driver, benchmark, leak/rumor phần cứng. "
            "BỎ: game thuần, console, phim/giải trí, crypto, tài chính, deal."
        ),
    },
    {
        "name":  "9to5Mac",
        "url":   "https://9to5mac.com/feed/",
        "count": 2,
        "focus": (
            "CHỈ máy tính Mac: MacBook, iMac, Mac mini, Mac Studio, Apple Silicon cho Mac, macOS. "
            "BỎ: iPhone, iPad, Apple Watch, dịch vụ/subscription, phụ kiện không liên quan Mac."
        ),
    },
]

HOURS_LOOKBACK   = 48
FETCH_PER_SOURCE = 15   # lấy nhiều để model có đủ để chọn sau khi lọc


# ── Fetch RSS ─────────────────────────────────────────────────────────────────
def fetch_source(source: dict) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_LOOKBACK)
    articles = []
    try:
        feed = feedparser.parse(source["url"])
        for entry in feed.entries[:FETCH_PER_SOURCE * 3]:
            pub = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

            # bỏ bài quá cũ (nếu có ngày)
            if pub and pub < cutoff:
                continue

            summary = getattr(entry, "summary", "") or ""
            summary = re.sub(r"<[^>]+>", "", summary)[:400].strip()

            articles.append({
                "source":  source["name"],
                "title":   entry.get("title", "").strip(),
                "url":     entry.get("link", ""),
                "summary": summary,
                "pub":     pub.isoformat() if pub else "unknown",
                "pub_dt":  pub,
            })

            if len(articles) >= FETCH_PER_SOURCE * 2:
                break
    except Exception as e:
        print(f"[WARN] {source['name']}: {e}")

    # sắp xếp mới → cũ
    articles.sort(key=lambda a: a["pub_dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return articles[:FETCH_PER_SOURCE]


# ── Prompt ────────────────────────────────────────────────────────────────────
def build_prompt(all_articles: dict[str, list[dict]], today: str) -> str:
    source_blocks = []
    for src in SOURCES:
        articles = all_articles.get(src["name"], [])
        block = f"=== {src['name']} ===\nLọc theo: {src['focus']}\n"
        for i, a in enumerate(articles, 1):
            block += f"\n{i}. [{a['pub']}] {a['title']}\n   {a['summary']}\n   URL: {a['url']}"
        source_blocks.append(block)

    articles_text = "\n\n".join(source_blocks)

    return f"""Ngày hôm nay: {today}

Dưới đây là danh sách bài báo công nghệ mới nhất từ 5 nguồn, mỗi nguồn đã được chú thích tiêu chí lọc.

{articles_text}

---

Nhiệm vụ của mày:

1. Từ mỗi nguồn, chọn ĐÚNG 2 bài phù hợp nhất với tiêu chí lọc đã nêu, sắp xếp mới → cũ.
   Tổng: 10 bài, giữ nguyên thứ tự nhóm nguồn: Android Authority → Windows Central → PCWorld → Wccftech → 9to5Mac.

2. Kiểm tra trùng lặp: nếu 2 nguồn cùng đưa tin về cùng một sự kiện, chỉ giữ 1, thay bài kia bằng bài tiếp theo của chính nguồn đó (vẫn giữ 2 bài/nguồn).

3. Viết tóm tắt tiếng Việt cho từng bài:
   - Tiêu đề ngắn gọn, rõ trọng tâm kỹ thuật
   - 1–2 câu tóm tắt, súc tích, không thêm phân tích/suy đoán ngoài bài gốc
   - Phong cách tin tức, dễ đọc, hướng tới độc giả hiểu công nghệ
   - Dùng "nhân" thay vì "lõi" (CPU/GPU/vi xử lý)
   - Dùng "tốc độ làm tươi" thay vì "tần số quét"

4. Xuất ra ĐÚNG format sau, không thêm bất kỳ text nào khác ngoài format:

🔥 Tin công nghệ sáng nay – {today}

[Tiêu đề tiếng Việt]
[1–2 câu tóm tắt]
Nguồn: [tên nguồn] – [URL gốc]

[lặp lại cho đủ 10 tin, mỗi tin cách nhau 1 dòng trống]"""


# ── Gọi Gemini với fallback ───────────────────────────────────────────────────
def build_digest(all_articles: dict[str, list[dict]]) -> str:
    today = datetime.now(ICT).strftime("%d/%m/%Y")
    prompt = build_prompt(all_articles, today)

    last_err = None
    for model in MODEL_CHAIN:
        # Gemini 3.x có thinking_level; 2.5 không có → chỉ gửi config khi cần
        config = None
        if model.startswith("gemini-3"):
            config = types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="low"),
            )

        for attempt in range(2):
            try:
                print(f"  → Gọi {model} (lần {attempt + 1})...")
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                )
                text = (resp.text or "").strip()
                if text:
                    print(f"  ✅ {model} OK")
                    return text
                raise RuntimeError("response rỗng")
            except Exception as e:
                last_err = e
                msg = str(e)
                print(f"  [WARN] {model}: {msg[:200]}")
                # 429 = rate limit → chờ rồi thử lại 1 lần, sau đó đổi model
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    time.sleep(30)
                    continue
                break  # lỗi khác → sang model tiếp theo

    raise RuntimeError(f"Tất cả model đều lỗi. Lỗi cuối: {last_err}")


# ── Gửi Telegram ─────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Telegram giới hạn 4096 ký tự/message – cắt nếu cần
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]

    for chunk in chunks:
        for parse_mode in ["Markdown", None]:
            payload = {
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     chunk,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode

            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                break
            else:
                err = resp.json().get("description", resp.text)
                if parse_mode:
                    print(f"[WARN] Telegram Markdown lỗi: {err} – thử plain text...")
                else:
                    print(f"[ERROR] Telegram thất bại: {err}")
                    return False
    return True


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now(ICT).strftime('%Y-%m-%d %H:%M ICT')}] Bắt đầu...")

    all_articles: dict[str, list[dict]] = {}
    for src in SOURCES:
        articles = fetch_source(src)
        all_articles[src["name"]] = articles
        print(f"  {src['name']}: {len(articles)} bài")

    print("  → Đang gọi Gemini để chọn lọc + viết bản tin...")
    digest = build_digest(all_articles)
    print(f"  → Digest ({len(digest)} ký tự):\n{digest[:300]}...\n")

    print("  → Gửi Telegram...")
    ok = send_telegram(digest)
    if not ok:
        raise RuntimeError("❌ Gửi Telegram thất bại – xem log ở trên.")
    print("  ✅ Gửi thành công.")


if __name__ == "__main__":
    main()
