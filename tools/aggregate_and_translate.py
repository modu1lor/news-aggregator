import os, json, time, hashlib
import feedparser, requests
from langdetect import detect
from dateutil import parser as dtp
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
DOCS_PATH = os.path.join(REPO_ROOT, "docs")
FEED_OUT = os.path.join(DOCS_PATH, "feed.json")
FEEDS_TXT = os.path.join(REPO_ROOT, "feeds.txt")

TARGET_LANG = os.getenv("TARGET_LANG", "ja")
LT_URL = os.getenv("LT_URL", "").rstrip("/")
LT_API_KEY = os.getenv("LT_API_KEY", "")

def norm_dt(entry):
    for k in ("published", "updated", "created"):
        if getattr(entry, k, None):
            try:
                return dtp.parse(getattr(entry, k)).astimezone(timezone.utc).isoformat()
            except Exception:
                pass
    if getattr(entry, "published_parsed", None):
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()

def identity(item):
    base = item.get("link") or item.get("id") or (item.get("title","") + item.get("source",""))
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()

def maybe_translate(text, src_hint=None):
    # 翻訳オフ（LT_URL未設定）なら原文を返す
    if not text or not LT_URL:
        return text, None
    try:
        lang = src_hint or detect(text)
        if lang == TARGET_LANG:
            return text, lang
    except Exception:
        lang = None
    try:
        payload = {"q": text, "source": "auto", "target": TARGET_LANG, "format": "text"}
        if LT_API_KEY:
            payload["api_key"] = LT_API_KEY
        r = requests.post(f"{LT_URL}/translate", json=payload, timeout=12)
        if r.ok:
            return r.json().get("translatedText", text), lang
    except Exception:
        pass
    return text, lang

def clamp(s, n=280):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n-1] + "…"

def main():
    os.makedirs(DOCS_PATH, exist_ok=True)
    with open(FEEDS_TXT, "r", encoding="utf-8") as f:
        feed_urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    items = []
    for url in feed_urls:
        try:
            d = feedparser.parse(url)
            src_title = (d.feed.title if "feed" in d and "title" in d.feed else url)
            for e in d.entries:
                title = getattr(e, "title", "")
                link = getattr(e, "link", "")
                summary = getattr(e, "summary", "") or getattr(e, "description", "")
                published = norm_dt(e)
                item = {"source": src_title, "title": clamp(title, 220), "summary": clamp(summary, 500),
                        "link": link, "published": published}
                items.append(item)
        except Exception:
            continue
        time.sleep(0.3)  # 過負荷対策

    seen = set()
    uniq = []
    for it in items:
        key = identity(it)
        if key in seen: continue
        seen.add(key)
        uniq.append(it)

    uniq.sort(key=lambda x: x.get("published",""), reverse=True)

    translated = []
    for it in uniq:
        t_title, lang1 = maybe_translate(it["title"])
        t_sum, lang2 = maybe_translate(it["summary"], src_hint=lang1)
        it["title_translated"] = t_title
        it["summary_translated"] = t_sum
        it["lang_detected"] = lang1 or lang2
        translated.append(it)
        if LT_URL: time.sleep(0.4)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_lang": TARGET_LANG,
        "count": len(translated),
        "items": translated[:1000]
    }
    with open(FEED_OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
