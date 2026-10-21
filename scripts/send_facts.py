#!/usr/bin/env python3
"""Gửi bài Tinhte Fact chờ duyệt (facts/latest.json) lên Telegram qua bot Peter.

Định dạng file (trạm đăng bài ghi mỗi sáng):
{
  "date": "YYYY-MM-DD",
  "station_url": "https://claude.ai/...",   # link cuộc trò chuyện trạm để bấm vào duyệt
  "notes": ["ghi chú tùy chọn", ...],
  "articles": [
    {"title": "...", "body": "...", "source": "...", "hot": 5, "reason": "...",
     "images_note": "1 ảnh từ ...", "urls": ["..."]}
  ]
}
Gửi tin dạng văn bản thường (không parse_mode) để khỏi lỗi ký tự đặc biệt.
"""
import json, os, sys, time
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]
DEFAULT_STATION = "https://claude.ai/code/session_01UBAPmPNPiTxvyrST1JLPxz"
LIMIT = 4000  # Telegram tối đa 4096 ký tự mỗi tin


def send(text: str) -> None:
    for i in range(0, len(text), LIMIT):
        chunk = text[i:i + LIMIT]
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": chunk, "disable_web_page_preview": True},
            timeout=30,
        )
        if r.status_code == 429:
            time.sleep(int(r.json().get("parameters", {}).get("retry_after", 5)) + 1)
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": CHAT, "text": chunk, "disable_web_page_preview": True},
                timeout=30,
            )
        r.raise_for_status()
        time.sleep(1)


def main(path: str) -> None:
    data = json.load(open(path, encoding="utf-8"))
    arts = data.get("articles", [])
    if not arts:
        print("Không có bài nào, bỏ qua.")
        return
    station = data.get("station_url") or DEFAULT_STATION

    lines = [f"📝 Tinhte Fact {data.get('date', '')}: {len(arts)} bài chờ duyệt", ""]
    for n, a in enumerate(arts, 1):
        hot = f" · hot {a['hot']}" if a.get("hot") else ""
        lines.append(f"{n}. {a.get('title', '').strip()}")
        meta = f"   Nguồn: {a.get('source', '')}{hot}"
        if a.get("reason"):
            meta += f" · {a['reason']}"
        lines.append(meta)
        if a.get("images_note"):
            lines.append(f"   Ảnh: {a['images_note']}")
    for note in data.get("notes", []) or []:
        lines += ["", f"⚠️ {note}"]
    lines += ["", "👉 Duyệt ở đây (nhắn 'đăng hết', 'đăng 1,3,5' hoặc bảo sửa bài nào):", station]
    send("\n".join(lines))

    for n, a in enumerate(arts, 1):
        send(f"━━ BÀI {n} ━━\n\n{a.get('title', '').strip()}\n\n{a.get('body', '').strip()}")

    send(f"Hết {len(arts)} bài. Duyệt: {station}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "facts/latest.json")
