# tools/aggregate_and_translate.py
# ------------------------------------------------------------
# GitHub Actions から実行して、
#  - feeds.txt のRSSを取得
#  - 重複排除・整形
#  - （任意）LibreTranslate で title/summary を翻訳
#  - Base44推奨フォーマットの feed.json を docs/ に出力
# ------------------------------------------------------------

import os
import json
import time
import hashlib
from urllib.parse import urlparse

import feedparser
import requests
from langdetect import detect
from dateutil import parser as dtp
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
DOCS_PATH = os.path.join(REPO_ROOT, "docs")
FEED_OUT = os.path.join(DOCS_PATH, "feed.json")
FEEDS_TXT = os.path.join(REPO_ROOT, "feeds.txt")

TARGET_LANG = os.getenv("TARGET_LANG", "ja")  # 翻訳のターゲット言語（デフォルトja）
LT_URL = os.getenv("LT_URL", "").rstrip("/")  # LibreTranslate のURL（未設定なら翻訳OFF）
LT_API_KEY = os.getenv("LT_API_KEY", "")      # LibreTranslateのAPIキー（無くても可）

# --- 地域推定: ccTLD(最終ラベル) → (国, 大陸)（日本語表記）
COUNTRY_MAP = {
    "us": ("アメリカ", "北アメリカ"),
    "ca": ("カナダ", "北アメリカ"),
    "mx": ("メキシコ", "北アメリカ"),
    "br": ("ブラジル", "南アメリカ"),
    "ar": ("アルゼンチン", "南アメリカ"),
    "uk": ("イギリス", "ヨーロッパ"),
    "de": ("ドイツ", "ヨーロッパ"),
    "fr": ("フランス", "ヨーロッパ"),
    "es": ("スペイン", "ヨーロッパ"),
    "it": ("イタリア", "ヨーロッパ"),
    "nl": ("オランダ", "ヨーロッパ"),
    "se": ("スウェーデン", "ヨーロッパ"),
    "no": ("ノルウェー", "ヨーロッパ"),
    "dk": ("デンマーク", "ヨーロッパ"),
    "pl": ("ポーランド", "ヨーロッパ"),
    "pt": ("ポルトガル", "ヨーロッパ"),
    "ie": ("アイルランド", "ヨーロッパ"),
    "ch": ("スイス", "ヨーロッパ"),
    "ru": ("ロシア", "ヨーロッパ/アジア"),
    "tr": ("トルコ", "ヨーロッパ/アジア"),
    "ua": ("ウクライナ", "ヨーロッパ"),
    "cz": ("チェコ", "ヨーロッパ"),
    "hu": ("ハンガリー", "ヨーロッパ"),
    "ro": ("ルーマニア", "ヨーロッパ"),
    "gr": ("ギリシャ", "ヨーロッパ"),
    "fi": ("フィンランド", "ヨーロッパ"),
    "cn": ("中国", "アジア"),
    "jp": ("日本", "アジア"),
    "kr": ("韓国", "アジア"),
    "tw": ("台湾", "アジア"),
    "hk": ("香港", "アジア"),
    "sg": ("シンガポール", "アジア"),
    "in": ("インド", "アジア"),
    "id": ("インドネシア", "アジア"),
    "th": ("タイ", "アジア"),
    "vn": ("ベトナム", "アジア"),
    "my": ("マレーシア", "アジア"),
    "ph": ("フィリピン", "アジア"),
    "au": ("オーストラリア", "オセアニア"),
    "nz": ("ニュージーランド", "オセアニア"),
    "za": ("南アフリカ", "アフリカ"),
    "ng": ("ナイジェリア", "アフリカ"),
    "eg": ("エジプト", "アフリカ"),
    "ke": ("ケニア", "アフリカ"),
    "sa": ("サウジアラビア", "中東"),
    "ae": ("アラブ首長国連邦", "中東"),
    "ir": ("イラン", "中東"),
    "il": ("イスラエル", "中東"),
    # 必要に応じて追加
}

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
            # 最初のタグの term or label を採用
            tag = entry.tags[0]
            for k in ("term", "label"):
                if k in tag and tag[k]:
                    return str(tag[k])[:64]
    except Exception:
        pass
    return "general"

def guess_region(link):
    """URLのTLDから (国, 大陸) を推定。失敗時は '不明'。"""
    try:
        netloc = urlparse(link).netloc.lower()
        # 例: www.bbc.co.uk -> tld 'uk'; nytimes.com -> 'com'
        tld = netloc.split(".")[-1]
        # com/net/org などは国判定できない
        if tld in COUNTRY_MAP:
            return COUNTRY_MAP[tld]
        # 例外的に co.uk / com.au などの最後が2文字じゃないケースは最後を優先
        # すでに tld は最後ラベルなので、ここではこれ以上の分解はしない
    except Exception:
        pass
    return ("不明", "不明")

def estimate_reading_time(text):
    """要約/本文の語数から読了時間(分)を概算。最低1分。"""
    words = len((text or "").split())
    return max(1, round(words / 200))  # 200 wpm を仮定

def main():
    os.makedirs(DOCS_PATH, exist_ok=True)

    # --- フィードリスト読込
    with open(FEEDS_TXT, "r", encoding="utf-8") as f:
        feed_urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

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
        time.sleep(0.3)  # 取得間隔（優しめに）

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
        # タイトルで判定した言語をサマリへ引き継ぎ（無ければサマリで自動判定）
        t_sum, lang2 = maybe_translate(it["summary"], src_hint=lang1)
        detected = lang1 or lang2

        it["title_translated"] = t_title
        it["summary_translated"] = t_sum
        it["lang_detected"] = detected  # ISOコード（例: en, fr）

        translated.append(it)
        if LT_URL:
            time.sleep(0.4)  # 無料API配慮

    # --- Base44 推奨スキーマに整形
    formatted = []
    for it in translated:
        country, continent = guess_region(it["link"])

        # 読了時間は「翻訳済み要約 > 原文要約 > タイトル」で概算
        basis = it.get("summary_translated") or it.get("summary") or it.get("title")
        reading_time = estimate_reading_time(basis)

        formatted.append({
            "title": it.get("title", ""),
            "title_translated": it.get("title_translated", "") or it.get("title", ""),
            "summary": it.get("summary", ""),
            "summary_translated": it.get("summary_translated", "") or it.get("summary", ""),
            "link": it.get("link", ""),
            "source_name": it.get("source", ""),
            "published": it.get("published", ""),
            "language": (it.get("lang_detected") or "unknown"),
            "country": country,
            "continent": continent,
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
