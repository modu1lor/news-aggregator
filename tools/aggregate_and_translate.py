# tools/aggregate_and_translate.py
# ------------------------------------------------------------
# - feeds.txt のRSSを取得
# - 重複排除・整形
# - （任意）LibreTranslateで title/summary を翻訳（未設定ならオフ）
# - Base44推奨スキーマで docs/feed.json を出力
# - data/news_sources.csv を使って country/continent/language を付与
# ------------------------------------------------------------

import os
import json
import time
import hashlib
import csv
from urllib.parse import urlparse

import feedparser
import requests
from langdetect import detect
from dateutil import parser as dtp
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
DOCS_PATH = os.path.join(REPO_ROOT, "docs")
DATA_PATH = os.path.join(REPO_ROOT, "data")
CATALOG_CSV = os.path.join(DATA_PATH, "news_sources.csv")

FEED_OUT = os.path.join(DOCS_PATH, "feed.json")
FEEDS_TXT = os.path.join(REPO_ROOT, "feeds.txt")

TARGET_LANG = os.getenv("TARGET_LANG", "ja")  # 翻訳のターゲット言語（デフォルトja）
LT_URL = os.getenv("LT_URL", "").rstrip("/")  # LibreTranslate のURL（未設定なら翻訳OFF）
LT_API_KEY = os.getenv("LT_API_KEY", "")      # LibreTranslateのAPIキー（無くても可）


def norm_dt(entry):
    """RSSエントリからISO8601(UTC)の日時文字列を得る。なければ現在時刻。"""
    for k in ("published", "updated", "created"):
        val = getattr(entry, k, None)
        if val:
            try:
                return dtp.parse(val).astimezone(timezone.utc).isoformat()
            except Exception:
                pass
    if getattr(entry, "published_parsed", None):
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def identity(item):
    """重複排除用ハッシュ。link > id > タイトル+ソース。"""
    base = item.get("link") or item.get("id") or (item.get("title","") + item.get("source",""))
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def maybe_translate(text, src_hint=None):
    """LibreTranslate が設定されていれば翻訳。未設定/失敗時は原文返し。"""
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


def extract_category(entry):
    """feedparserのtags等からカテゴリを抽出。無ければ 'general'。"""
    try:
        if hasattr(entry, "tags") and entry.tags:
            tag = entry.tags[0]
            for k in ("term", "label"):
                if k in tag and tag[k]:
                    return str(tag[k])[:64]
    except Exception:
        pass
    return "general"


# ========= ここから：CSVカタログの読み込み & 照合 =========

def _netloc(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def load_source_catalog(csv_path):
    """
    data/news_sources.csv を読み、以下の2種類のインデックスを作る:
      - domain_map: 公式サイトのドメイン -> 行(dict)
      - name_map:   媒体名(小文字)       -> 行(dict)
    期待される列: name, country, continent, language, website_url, city, flag_emoji, political_stance, economic_stance
    """
    domain_map = {}
    name_map = {}
    if not os.path.exists(csv_path):
        return domain_map, name_map

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            # name マップ
            name_map[name.lower()] = row
            # domain マップ（website_url があればドメイン抽出）
            site = (row.get("website_url") or "").strip()
            if site:
                d = _netloc(site)
                if d:
                    domain_map[d] = row
    return domain_map, name_map


def enrich_from_catalog(source_name: str, link_url: str, domain_map, name_map):
    """
    リンクのドメイン -> catalog 照合、ダメなら source_name の部分一致/完全一致で照合。
    戻り値: (country, continent, language, catalog_name)  いずれも無ければ None
    """
    # 1) ドメイン一致（最も信頼度高）
    d = _netloc(link_url)
    if d and d in domain_map:
        row = domain_map[d]
        return (
            (row.get("country") or "").strip() or None,
            (row.get("continent") or "").strip() or None,
            (row.get("language") or "").strip() or None,
            (row.get("name") or "").strip() or None,
        )

    # 2) 媒体名で一致（小文字完全一致）
    key = (source_name or "").strip().lower()
    if key in name_map:
        row = name_map[key]
        return (
            (row.get("country") or "").strip() or None,
            (row.get("continent") or "").strip() or None,
            (row.get("language") or "").strip() or None,
            (row.get("name") or "").strip() or None,
        )

    # 3) 部分一致（例: "The Guardian - World" に "the guardian" を含む）
    for nm, row in name_map.items():
        if nm and nm in key:
            return (
                (row.get("country") or "").strip() or None,
                (row.get("continent") or "").strip() or None,
                (row.get("language") or "").strip() or None,
                (row.get("name") or "").strip() or None,
            )

    return None, None, None, None


# ========= ここまで：CSVカタログ =========


def estimate_reading_time(text):
    """要約/本文の語数から読了時間(分)を概算。最低1分。"""
    words = len((text or "").split())
    return max(1, round(words / 200))  # 200 wpm を仮定


def main():
    os.makedirs(DOCS_PATH, exist_ok=True)

    # --- フィードリスト読込
    with open(FEEDS_TXT, "r", encoding="utf-8") as f:
        feed_urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    # --- カタログ読込（存在しなくてもOK）
    domain_map, name_map = load_source_catalog(CATALOG_CSV)

    # --- 収集
    raw_items = []
    for url in feed_urls:
        try:
            d = feedparser.parse(url)
            src_title = (d.feed.title if "feed" in d and "title" in d.feed else url)
            for e in d.entries:
                title = getattr(e, "title", "")
                link = getattr(e, "link", "")
                summary = getattr(e, "summary", "") or getattr(e, "description", "")
                published = norm_dt(e)
                category = extract_category(e)
                raw_items.append({
                    "source": src_title,
                    "title": clamp(title, 220),
                    "summary": clamp(summary, 1200),
                    "link": link,
                    "published": published,
                    "category": category,
                })
        except Exception:
            # フィードごとの失敗は無視して続行
            pass
        time.sleep(0.3)  # 取得間隔（やさしめ）

    # --- 重複排除（link中心）
    seen = set()
    uniq = []
    for it in raw_items:
        key = identity(it)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)

    # --- 新しい順にソート
    uniq.sort(key=lambda x: x.get("published", ""), reverse=True)

    # --- 翻訳（タイトル・要約）
    translated = []
    for it in uniq:
        t_title, lang1 = maybe_translate(it["title"])
        t_sum, lang2 = maybe_translate(it["summary"], src_hint=lang1)
        detected = lang1 or lang2  # 'en','fr'のようなISOコードが入る可能性

        it["title_translated"] = t_title
        it["summary_translated"] = t_sum
        it["lang_detected"] = detected

        translated.append(it)
        if LT_URL:
            time.sleep(0.4)  # 無料API配慮

    # --- Base44 推奨スキーマに整形（CSVカタログで国/大陸/言語を付与）
    formatted = []
    for it in translated:
        # CSVカタログで enrich
        cat_country, cat_continent, cat_lang, cat_name = enrich_from_catalog(
            source_name=it.get("source", ""),
            link_url=it.get("link", ""),
            domain_map=domain_map,
            name_map=name_map,
        )

        # language優先順位: CSVの日本語表記(例: "英語") > 自動検出(ISOコード) > "unknown"
        language_value = cat_lang or (it.get("lang_detected") or "unknown")

        # source_name は CSVにある場合はCSVの name を優先（なければ feed の source）
        source_name = cat_name or it.get("source", "")

        # 読了時間は翻訳済み要約→原文要約→タイトルで概算
        basis = it.get("summary_translated") or it.get("summary") or it.get("title")
        reading_time = estimate_reading_time(basis)

        formatted.append({
            "title": it.get("title", ""),
            "title_translated": it.get("title_translated", "") or it.get("title", ""),
            "summary": it.get("summary", ""),
            "summary_translated": it.get("summary_translated", "") or it.get("summary", ""),
            "link": it.get("link", ""),
            "source_name": source_name,
            "published": it.get("published", ""),
            "language": language_value,           # 例: CSVなら「英語」などの日本語。無ければ 'en' など。
            "country": cat_country or "不明",
            "continent": cat_continent or "不明",
            "category": it.get("category", "general"),
            "reading_time_minutes": int(reading_time),
        })

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_lang": TARGET_LANG,
        "count": len(formatted),
        "items": formatted[:1000],  # 念のため上限
    }

    with open(FEED_OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
