#!/usr/bin/env python3
"""
Tech News Digest Bot – bản "tin hot"
Gửi Telegram lúc 5:00 ICT, chọn 10 tin có độ hot/traffic cao nhất trong ~36 giờ qua.

Cách đo độ hot (không cần API key):
  1. Độ phủ: cùng một sự kiện được bao nhiêu trang tin lớn cùng đăng
  2. Techmeme: tin có lên trang nhất Techmeme không
  3. Hacker News: điểm + số bình luận của bài liên quan
  4. Google Trends (US + VN): từ khoá đang được tìm kiếm nhiều, kèm lượng tìm kiếm ước tính
Python tính điểm sơ bộ + gom cụm tin trùng, Gemini xếp hạng cuối và viết tóm tắt.

Ngoài tin nhắn Telegram, bot ghi thêm:
  digests/YYYY-MM-DD.json  – dữ liệu đầy đủ (URL nguồn, các URL cùng sự kiện, mức hot)
  digests/latest.json      – bản mới nhất, để bước viết tin 6:00 đọc
"""

import os, re, sys, json, math, time, html, glob
import feedparser, requests
from google import genai
from google.genai import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]
FORCE              = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")

client = genai.Client(api_key=GEMINI_API_KEY)
ICT = ZoneInfo("Asia/Bangkok")
UA = {"User-Agent": "Mozilla/5.0 (compatible; tech-digest-bot/2.0; +https://github.com/pnghuy/tech-digest)"}

MODEL_CHAIN = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash"]

HOURS_LOOKBACK   = 36
ITEMS_PER_FEED   = 40
TOP_N            = 10
MAX_CANDIDATES   = 70     # số cụm tin gửi cho Gemini chọn
HISTORY_DAYS     = 3      # không lặp lại tin đã gửi trong 3 ngày
DIGEST_DIR       = "digests"

# ── Nguồn tin (đều là trang quốc tế) ─────────────────────────────────────────
# 5 nguồn cũ vẫn giữ, thêm các trang lớn để đo độ phủ của một sự kiện.
SOURCES = [
    ("Android Authority", "https://www.androidauthority.com/feed/"),
    ("9to5Google",        "https://9to5google.com/feed/"),
    ("GSMArena",          "https://www.gsmarena.com/rss-news-reviews.php3"),
    ("Windows Central",   "https://www.windowscentral.com/feed"),
    ("Neowin",            "https://www.neowin.net/news/rss/"),
    ("PCWorld",           "https://www.pcworld.com/index.rss"),
    ("Wccftech",          "https://wccftech.com/feed/"),
    ("Tom's Hardware",    "https://www.tomshardware.com/feeds/all"),
    ("VideoCardz",        "https://videocardz.com/feed"),
    ("9to5Mac",           "https://9to5mac.com/feed/"),
    ("MacRumors",         "https://feeds.macrumors.com/MacRumors-All"),
    ("The Verge",         "https://www.theverge.com/rss/index.xml"),
    ("Engadget",          "https://www.engadget.com/rss.xml"),
    ("Ars Technica",      "https://feeds.arstechnica.com/arstechnica/index"),
    ("TechCrunch",        "https://techcrunch.com/feed/"),
]

# Bỏ ngay ở bước Python: deal, hướng dẫn, best-of, podcast…
JUNK_RE = re.compile(
    r"(\bdeals?\b|\b\d+% off\b|\bsale\b|save \$|\bcoupon|\bprime day\b|black friday|cyber monday|"
    r"^how to\b|\bhow to\b.*\b(fix|use|turn|set|get|enable|disable)\b|\bbest\b.+\b(for|to buy|of 20\d\d)\b|"
    r"\bpodcast\b|\bweekly roundup\b|\bthis week in\b|\bnewsletter\b|\bwordle\b|\bconnections hints?\b|"
    r"\bsponsored\b|\bgiveaway\b|\bquiz\b)",
    re.I,
)

STOP = set("""
a an the and or but of to in on for with at by from as is are was were be been being it its this that these those
will would can could may might should has have had do does did not no new now just more most than then into over
after before about your you our we they their his her he she them what when where who why how which also up out
get gets got make makes made says said report reports reportedly could here there first one two vs via all any
""".split())


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9\-\+\.]*", text.lower())
    out = set()
    for w in words:
        w = w.strip(".-+")
        if not w or w in STOP:
            continue
        if len(w) >= 3 or any(c.isdigit() for c in w):
            out.add(w)
    return out


def similar(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    shared = len(a & b)
    return shared >= 3 and shared / min(len(a), len(b)) >= 0.45


def clean(s: str, n: int = 350) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()[:n]


def get(url: str, **kw):
    return requests.get(url, headers=UA, timeout=20, **kw)


def entry_time(e) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(e, key, None) or (e.get(key) if hasattr(e, "get") else None)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


# ── 1. Lấy bài từ các trang tin ──────────────────────────────────────────────
def fetch_sources(cutoff: datetime) -> list[dict]:
    arts = []
    for name, url in SOURCES:
        n = 0
        try:
            feed = feedparser.parse(get(url).content)
            for e in feed.entries[:ITEMS_PER_FEED]:
                pub = entry_time(e)
                if pub and pub < cutoff:
                    continue
                title = clean(e.get("title", ""), 300)
                if not title or JUNK_RE.search(title):
                    continue
                arts.append({
                    "source": name, "title": title, "url": e.get("link", ""),
                    "summary": clean(getattr(e, "summary", "")),
                    "pub": pub, "tok": tokens(title),
                })
                n += 1
        except Exception as ex:
            print(f"  [WARN] {name}: {ex}")
        print(f"  {name}: {n} bài")
    return arts


# ── 2. Tín hiệu độ hot ───────────────────────────────────────────────────────
def fetch_techmeme(cutoff: datetime) -> list[dict]:
    out = []
    try:
        feed = feedparser.parse(get("https://www.techmeme.com/feed.xml").content)
        for e in feed.entries:
            pub = entry_time(e)
            if pub and pub < cutoff:
                continue
            title = clean(e.get("title", ""), 300)
            title = re.sub(r"\s*\([^()]*\)\s*$", "", title)   # bỏ "(Tác giả/Báo)" cuối tiêu đề
            out.append({"title": title, "tok": tokens(title)})
    except Exception as ex:
        print(f"  [WARN] Techmeme: {ex}")
    print(f"  Techmeme: {len(out)} tin")
    return out


def fetch_hn(cutoff: datetime) -> list[dict]:
    out = []
    try:
        r = get("https://hn.algolia.com/api/v1/search_by_date", params={
            "tags": "story", "hitsPerPage": 200,
            "numericFilters": f"created_at_i>{int(cutoff.timestamp())},points>40",
        })
        for h in r.json().get("hits", []):
            title = h.get("title") or ""
            out.append({"title": title, "url": h.get("url") or "", "points": h.get("points") or 0,
                        "comments": h.get("num_comments") or 0, "tok": tokens(title)})
    except Exception as ex:
        print(f"  [WARN] Hacker News: {ex}")
    print(f"  Hacker News: {len(out)} bài")
    return out


def parse_traffic(s: str) -> int:
    s = (s or "").replace(",", "").replace("+", "").strip().upper()
    mult = 1
    if s.endswith("K"): mult, s = 1_000, s[:-1]
    if s.endswith("M"): mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def fetch_trends() -> list[dict]:
    out = []
    for geo in ("US", "VN"):
        try:
            feed = feedparser.parse(get(f"https://trends.google.com/trending/rss?geo={geo}").content)
            for e in feed.entries:
                term = clean(e.get("title", ""), 100)
                traffic = parse_traffic(e.get("ht_approx_traffic", ""))
                news = e.get("ht_news_item_title", "") or ""
                out.append({"geo": geo, "term": term, "traffic": traffic,
                            "tok": tokens(term + " " + news)})
        except Exception as ex:
            print(f"  [WARN] Google Trends {geo}: {ex}")
    print(f"  Google Trends: {len(out)} từ khoá")
    return out


# ── 3. Gom cụm + chấm điểm ───────────────────────────────────────────────────
def cluster(arts: list[dict]) -> list[dict]:
    arts.sort(key=lambda a: a["pub"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    clusters: list[dict] = []
    for a in arts:
        for c in clusters:
            if similar(a["tok"], c["tok"]):
                c["arts"].append(a)
                c["tok"] |= a["tok"]
                break
        else:
            clusters.append({"arts": [a], "tok": set(a["tok"])})
    return clusters


def score(clusters, techmeme, hn, trends, now):
    for c in clusters:
        outlets = {a["source"] for a in c["arts"]}
        c["outlets"] = sorted(outlets)
        c["techmeme"] = any(similar(t["tok"], c["tok"]) for t in techmeme)
        urls = {a["url"] for a in c["arts"]}
        hn_hits = [h for h in hn if h["url"] in urls or similar(h["tok"], c["tok"])]
        c["hn_points"] = max((h["points"] for h in hn_hits), default=0)
        c["hn_comments"] = max((h["comments"] for h in hn_hits), default=0)
        tr = [t for t in trends if t["term"] and tokens(t["term"]) and tokens(t["term"]) <= c["tok"]]
        c["trends"] = [f'{t["term"]} ({t["geo"]}, ~{t["traffic"]:,} lượt tìm)' for t in tr]
        newest = max((a["pub"] for a in c["arts"] if a["pub"]), default=None)
        age_h = (now - newest).total_seconds() / 3600 if newest else 24
        c["age_h"] = round(age_h, 1)

        s = 2.0 * (len(outlets) - 1)
        s += 3.0 if c["techmeme"] else 0
        s += min(c["hn_points"] / 100, 4)
        s += sum(math.log10(max(t["traffic"], 10)) for t in tr)
        s += 1.0 if age_h <= 12 else 0
        c["score"] = round(s, 2)
    clusters.sort(key=lambda c: c["score"], reverse=True)
    return clusters


# ── 4. Lịch sử: không lặp tin đã gửi ─────────────────────────────────────────
def load_history(today_ict: datetime) -> tuple[set[str], list[str]]:
    urls, titles = set(), []
    for d in range(1, HISTORY_DAYS + 1):
        path = os.path.join(DIGEST_DIR, (today_ict - timedelta(days=d)).strftime("%Y-%m-%d") + ".json")
        if not os.path.exists(path):
            continue
        try:
            data = json.load(open(path, encoding="utf-8"))
            for it in data.get("items", []):
                urls.add(it.get("url", ""))
                urls.update(it.get("related_urls", []))
                titles.append(it.get("source_title", "") or it.get("title_vi", ""))
        except Exception as ex:
            print(f"  [WARN] đọc lịch sử {path}: {ex}")
    return urls, [t for t in titles if t]


# ── 5. Gemini xếp hạng + viết tóm tắt ────────────────────────────────────────
def build_prompt(cands: list[dict], recent_titles: list[str], today: str) -> str:
    blocks = []
    for i, c in enumerate(cands):
        sig = [f"điểm sơ bộ {c['score']}", f"{len(c['outlets'])} trang cùng đăng ({', '.join(c['outlets'])})"]
        if c["techmeme"]: sig.append("có trên Techmeme")
        if c["hn_points"]: sig.append(f"Hacker News {c['hn_points']} điểm/{c['hn_comments']} bình luận")
        if c["trends"]: sig.append("Google Trends: " + "; ".join(c["trends"][:3]))
        sig.append(f"tin mới nhất cách đây {c['age_h']} giờ")
        lines = [f"[{i}] " + " | ".join(sig)]
        for j, a in enumerate(c["arts"][:4]):
            lines.append(f"   ({j}) {a['source']}: {a['title']}\n       {a['summary'][:220]}\n       URL: {a['url']}")
        blocks.append("\n".join(lines))
    recent = "\n".join(f"- {t}" for t in recent_titles[:40]) or "(không có)"

    return f"""Ngày hôm nay: {today}. Bạn là biên tập viên tin nhanh của Tinhte.vn, trang công nghệ lớn ở Việt Nam.

Dưới đây là {len(cands)} cụm tin công nghệ trong {HOURS_LOOKBACK} giờ qua. Mỗi cụm là một sự kiện, kèm tín hiệu độ hot:
số trang lớn cùng đăng, có lên Techmeme không, điểm Hacker News, từ khoá Google Trends (US/VN) và lượng tìm kiếm.

{chr(10).join(blocks)}

Tin đã gửi trong {HISTORY_DAYS} ngày gần đây (KHÔNG chọn lại cùng sự kiện, trừ khi có diễn biến mới lớn):
{recent}

---
Nhiệm vụ: chọn ĐÚNG {TOP_N} sự kiện HOT NHẤT, tức là tin sốt dẻo, nhiều người tìm kiếm và đọc, dễ kéo traffic cho độc giả công nghệ Việt Nam.

Ưu tiên: ra mắt/rò rỉ smartphone, laptop, PC, chip CPU/GPU/NPU, Apple, Samsung, Google, Microsoft, Windows, macOS, Android, iOS,
Nvidia, AMD, Intel, Qualcomm, MediaTek, tai nghe/âm thanh, màn hình, đồng hồ thông minh, sản phẩm AI lớn (OpenAI, Google, Anthropic, Meta…),
sự cố/lỗ hổng bảo mật ảnh hưởng nhiều người dùng, tin lớn của các hãng công nghệ (sa thải, thâu tóm, cấm vận chip…).
Thương hiệu có mặt ở Việt Nam (Samsung, Apple, Xiaomi, OPPO, vivo, ASUS, MSI, Acer, Lenovo, Dell, HP…) được cộng điểm.
Loại bỏ: khuyến mãi/deal, hướng dẫn, review dài, best-of, bài ý kiến, tin thuần game/console, phim ảnh, crypto, chứng khoán, chính trị, tội phạm.

Cách chấm: tín hiệu độ hot là căn cứ chính (nhiều trang cùng đăng + Techmeme + Trends quan trọng hơn HN), sau đó đến mức độ quan tâm của độc giả Việt Nam.
Mỗi sự kiện chỉ chọn 1 lần. Sắp xếp từ HOT nhất xuống.

Với mỗi tin, trả về:
- cluster: số [i] của cụm
- article: số (j) của bài làm nguồn chính (ưu tiên trang gốc đưa tin, chi tiết nhất)
- title_vi: tiêu đề tiếng Việt ngắn gọn, rõ trọng tâm
- summary_vi: 1–2 câu tóm tắt, chỉ dựa trên nội dung đã cho, không suy đoán
- hot_level: 1–5 (5 = tin cực nóng mà trang công nghệ nào cũng phải đưa; 4 = rất hot; 3 = đáng đưa)
- hot_reason: 1 câu ngắn giải thích vì sao hot

Quy tắc chữ: dùng "nhân" thay "lõi" (CPU/GPU/vi xử lý), "tốc độ làm tươi" thay "tần số quét". Không dùng dấu chấm phẩy.
Chỉ trả về JSON dạng {{"items": [{{"cluster": 0, "article": 0, "title_vi": "", "summary_vi": "", "hot_level": 5, "hot_reason": ""}}]}}"""


def call_gemini(prompt: str) -> dict:
    last_err = None
    for model in MODEL_CHAIN:
        kw = {"response_mime_type": "application/json"}
        if model.startswith("gemini-3"):
            kw["thinking_config"] = types.ThinkingConfig(thinking_level="low")
        config = types.GenerateContentConfig(**kw)
        for attempt in range(2):
            try:
                print(f"  → Gọi {model} (lần {attempt + 1})...")
                resp = client.models.generate_content(model=model, contents=prompt, config=config)
                text = (resp.text or "").strip()
                text = re.sub(r"^```(?:json)?|```$", "", text).strip()
                data = json.loads(text)
                if data.get("items"):
                    print(f"  ✅ {model} OK")
                    return data
                raise RuntimeError("JSON không có items")
            except Exception as e:
                last_err = e
                msg = str(e)
                print(f"  [WARN] {model}: {msg[:200]}")
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    time.sleep(30)
                    continue
                if isinstance(e, (json.JSONDecodeError, RuntimeError)) and attempt == 0:
                    continue
                break
    raise RuntimeError(f"Tất cả model đều lỗi. Lỗi cuối: {last_err}")


# ── 6. Telegram (giữ nguyên format cũ) ───────────────────────────────────────
def render_telegram(items: list[dict], today: str) -> str:
    parts = [f"🔥 Tin công nghệ sáng nay – {today}"]
    for it in items:
        parts.append(f"{it['title_vi']}\n{it['summary_vi']}\nNguồn: {it['source']} – {it['url']}")
    return "\n\n".join(parts)


def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks, buf = [], ""
    for block in text.split("\n\n"):            # cắt theo tin, không cắt giữa tin
        if len(buf) + len(block) + 2 > 4000:
            chunks.append(buf); buf = block
        else:
            buf = f"{buf}\n\n{block}" if buf else block
    if buf: chunks.append(buf)

    for chunk in chunks:
        for parse_mode in ["Markdown", None]:
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "disable_web_page_preview": True}
            if parse_mode: payload["parse_mode"] = parse_mode
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                break
            err = resp.json().get("description", resp.text)
            if parse_mode:
                print(f"  [WARN] Telegram Markdown lỗi: {err} – thử plain text...")
            else:
                print(f"  [ERROR] Telegram thất bại: {err}")
                return False
    return True


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc)
    now_ict = now.astimezone(ICT)
    day = now_ict.strftime("%Y-%m-%d")
    today = now_ict.strftime("%d/%m/%Y")
    out_path = os.path.join(DIGEST_DIR, f"{day}.json")
    print(f"[{now_ict:%Y-%m-%d %H:%M} ICT] Bắt đầu...")

    if os.path.exists(out_path) and not FORCE:
        print(f"  Đã có bản tin {day} – bỏ qua (FORCE_RUN=1 để chạy lại).")
        return

    cutoff = now - timedelta(hours=HOURS_LOOKBACK)
    arts = fetch_sources(cutoff)
    techmeme, hn, trends = fetch_techmeme(cutoff), fetch_hn(cutoff), fetch_trends()

    sent_urls, recent_titles = load_history(now_ict)
    arts = [a for a in arts if a["url"] not in sent_urls]
    clusters = score(cluster(arts), techmeme, hn, trends, now)
    cands = clusters[:MAX_CANDIDATES]
    print(f"  → {len(arts)} bài, {len(clusters)} cụm, gửi {len(cands)} cụm cho Gemini")
    for c in cands[:10]:
        print(f"     {c['score']:>5}  {len(c['outlets'])} trang  {c['arts'][0]['title'][:90]}")

    data = call_gemini(build_prompt(cands, recent_titles, today))

    items, used = [], set()
    for it in data["items"]:
        try:
            ci = int(it["cluster"]); c = cands[ci]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if ci in used:
            continue
        used.add(ci)
        try:
            a = c["arts"][int(it.get("article", 0))]
        except (ValueError, IndexError, TypeError):
            a = c["arts"][0]
        items.append({
            "rank": len(items) + 1,
            "title_vi": it.get("title_vi", "").strip(),
            "summary_vi": it.get("summary_vi", "").strip(),
            "hot_level": int(it.get("hot_level", 3) or 3),
            "hot_reason": it.get("hot_reason", "").strip(),
            "source": a["source"], "url": a["url"], "source_title": a["title"],
            "related_urls": [x["url"] for x in c["arts"] if x["url"] != a["url"]][:4],
            "signals": {"outlets": c["outlets"], "techmeme": c["techmeme"],
                        "hn_points": c["hn_points"], "trends": c["trends"], "score": c["score"]},
        })
        if len(items) >= TOP_N:
            break
    if not items:
        raise RuntimeError("Gemini không trả về tin hợp lệ.")

    text = render_telegram(items, today)
    print(f"  → Digest ({len(items)} tin, {len(text)} ký tự)\n{text[:500]}...\n")

    os.makedirs(DIGEST_DIR, exist_ok=True)
    record = {"date": day, "generated_at": now_ict.isoformat(), "items": items}
    for p in (out_path, os.path.join(DIGEST_DIR, "latest.json")):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

    print("  → Gửi Telegram...")
    if not send_telegram(text):
        raise RuntimeError("❌ Gửi Telegram thất bại – xem log ở trên.")
    print("  ✅ Gửi thành công.")


if __name__ == "__main__":
    main()
