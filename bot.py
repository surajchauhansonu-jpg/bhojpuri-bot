"""Bhojpuri Express - news -> poster + caption + hashtags -> Facebook Page.
Run:  python bot.py          (DRY_RUN=1 in .env => sirf out/ folder me save, post nahi)
"""
import os, re, json, base64, random, sqlite3, time
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote, parse_qs
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
HERE = Path(__file__).parent
OUT = HERE / "out"; OUT.mkdir(exist_ok=True)

SOURCES = {
    "Aaj Tak":        "https://www.aajtak.in/entertainment/bhojpuri",
    "Live Hindustan": "https://www.livehindustan.com/tags/bhojpuri-news",
    "News18 Hindi":   "https://hindi.news18.com/news/bhojpuri-news/",
    "India TV":       "https://www.indiatv.in/topic/bhojpuri",
    "ABP Live":       "https://www.abplive.com/entertainment/bhojpuri-cinema",
}
PAGE_NAME   = os.getenv("PAGE_NAME", "Bhojpuri Express")
MAX_POSTS   = int(os.getenv("MAX_POSTS_PER_RUN", "2"))
DRY_RUN     = os.getenv("DRY_RUN", "1") == "1"
USE_IMG     = os.getenv("USE_ARTICLE_IMAGE", "1") == "1"
MODEL       = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")   # free-tier Flash model; naam badle to .env me change karo
GEMINI_KEY  = os.getenv("GEMINI_API_KEY")
FB_PAGE_ID  = os.getenv("FB_PAGE_ID")
FB_TOKEN    = os.getenv("FB_PAGE_TOKEN")
LOGO        = HERE / "logo.png"
MAX_AGE_H   = int(os.getenv("MAX_AGE_HOURS", "24"))        # isse purani khabar nahi lenge
MAX_TRIES   = int(os.getenv("MAX_TRIES_PER_RUN", "8"))     # ek run me max itne articles Gemini ko dikhayenge
STRICT_DATE = os.getenv("STRICT_DATE", "1") == "1"          # date pata na ho to khabar skip
QUERIES     = os.getenv("NEWS_QUERIES", "भोजपुरी सिनेमा|भोजपुरी फिल्म|भोजपुरी गाना|भोजपुरी अभिनेत्री|भोजपुरी इंडस्ट्री|पवन सिंह|खेसारी लाल यादव|निरहुआ|Bhojpuri cinema").split("|")
IST = timezone(timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 (compatible; BhojpuriExpressBot/1.0)"}

db = sqlite3.connect(HERE / "seen.db")
db.execute("CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY, ts REAL)")

db.execute("CREATE TABLE IF NOT EXISTS posted (headline TEXT, ts REAL)")

def save_headline(h): db.execute("INSERT INTO posted VALUES (?,?)", (h, time.time())); db.commit()

def recent_text(hours=48, n=25):
    rows = db.execute("SELECT headline FROM posted WHERE ts>? ORDER BY ts DESC LIMIT ?",
                      (time.time() - hours * 3600, n)).fetchall()
    return "\n".join("- " + r[0] for r in rows) or "(abhi koi nahi)"

def seen(url): return db.execute("SELECT 1 FROM seen WHERE url=?", (url,)).fetchone() is not None
def mark(url): db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (url, time.time())); db.commit()

# ---------- 1. News collect ----------
def listing_links(url, limit=8):
    html = requests.get(url, headers=UA, timeout=20).text
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(url).netloc
    found, out = set(), []
    for a in soup.find_all("a", href=True):
        href, text = urljoin(url, a["href"]).split("#")[0], a.get_text(" ", strip=True)
        if urlparse(href).netloc != host or len(text) < 30 or href in found or href == url:
            continue
        found.add(href); out.append(href)
        if len(out) >= limit: break
    return out

def parse_dt(v):
    if not v: return None
    v = v.strip()
    try: d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        try: d = parsedate_to_datetime(v)
        except Exception: return None
    return d if d.tzinfo else d.replace(tzinfo=IST)

def article_facts(url):
    html = requests.get(url, headers=UA, timeout=20).text
    soup = BeautifulSoup(html, "html.parser")
    meta = lambda p: (soup.find("meta", property=p) or soup.find("meta", attrs={"name": p}) or {}).get("content", "")
    root = soup.find("article") or soup
    plist = [p.get_text(" ", strip=True) for p in root.find_all("p")]
    paras = " ".join(t for t in plist if len(t) > 40)
    img = meta("og:image") or meta("twitter:image")
    pub = meta("article:published_time") or meta("og:article:published_time") or meta("datePublished")
    if not pub:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html); pub = m.group(1) if m else ""
    return {"title": meta("og:title"), "desc": meta("og:description"), "body": paras[:5000],
            "image": urljoin(url, img) if img else "", "published": pub}

def fetch_image_uri(img_url):
    try:
        r = requests.get(img_url, headers=UA, timeout=20)
        ct = r.headers.get("content-type", "").split(";")[0]
        if r.ok and ct.startswith("image/") and len(r.content) > 20000:
            return f"data:{ct};base64," + base64.b64encode(r.content).decode()
    except Exception as e:
        print("image download fail:", e)
    return None

class RateLimited(Exception): pass

# ---------- 2. Gemini (free tier): headline + caption + hashtags ----------
PROMPT = """Tum "{page}" Facebook page ke editor ho (Bhojpuri cinema/entertainment news).
Neeche ek khabar ke facts hain. Agar ye Bhojpuri cinema/music/celebrity se related nahi hai, ya facts kamzor hain, to {{"skip": true}} do. Agar ye khabar neeche wali "pehle post ho chuki khabrein" me se kisi ek ki hi same story hai (chaahe site alag ho), to bhi {{"skip": true}} do. Agar khabar 1 din se zyada purani hai, ya kisi purani ghatna ko dobara bata rahi hai, to bhi {{"skip": true}} do.
Warna sirf JSON do:
{{"skip": false,
 "headline": "poster ke liye Hindi headline, max 12 shabd, clickbait/jhooth nahi",
 "caption": "Facebook caption Hindi me, 400 se 700 characters, chhoti lines (har line alag, beech me khaali line). Structure: (1) pehli line me hook + ek emoji, (2-5) khabar ke mukhya points - kaun, kya hua, kab/kahan, kyun/background, kisne kya kaha, (6) ek line me aage kya ho sakta hai ya nishkarsh, aakhri line: 'आपकी राय कमेंट में बताएं 👇'. Sirf facts me likhi baatein, koi ateet/andaza nahi.",
 "hashtags": ["8 se 10 hashtags, Hindi/English mix, # ke saath. Sirf khabar me aaye naam/film/topic se jude hashtags + #BhojpuriNews #BhojpuriExpress. Jo naam khabar me nahi hai uska hashtag mat banao."]}}
Sirf facts me jo likha hai wahi use karo, kuch invent mat karo. Original article ke sentences copy mat karo.

Pehle post ho chuki khabrein (last 48 ghante):
{recent}

Aaj ki tareekh: {today}
Khabar ki publish date: {published}

Source: {src}
Title: {title}
Description: {desc}
Text: {body}"""

def make_content(src, facts):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": PROMPT.format(page=PAGE_NAME, src=src, recent=recent_text(), today=datetime.now(IST).strftime("%d %b %Y %H:%M IST"), **facts)}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7}}
    time.sleep(6)                                 # free tier ke per-minute limit se bachne ke liye
    for attempt in range(3):                      # 429 aaye to wait & retry
        r = requests.post(url, json=body, headers={"x-goog-api-key": GEMINI_KEY}, timeout=60)
        if r.status_code == 429:
            print(f"Gemini 429 (try {attempt+1}/3): {r.text[:300]}")
            time.sleep(30 * (attempt + 1)); continue
        r.raise_for_status(); break
    else:
        raise RateLimited("Gemini rate limit")
    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = re.search(r"\{.*\}", txt, re.S).group(0)
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:                  # kabhi kabhi galat \u escape aa jata hai
        fixed = re.sub(r"\\u(?![0-9a-fA-F]{4})", r"\\\\u", raw)
        return json.loads(fixed, strict=False)

# ---------- 3. Poster ----------
THEMES = [("#b30000", "#ff6a00"), ("#0b3d91", "#00b4d8"), ("#4a148c", "#e91e63")]
HTML = """<html><body style="margin:0"><div style="width:1080px;height:1350px;position:relative;overflow:hidden;
background:linear-gradient(135deg,{c1},{c2});font-family:'Noto Sans Devanagari','Hind','Nirmala UI',sans-serif;color:#fff">
<div style="position:absolute;right:-180px;top:-180px;width:560px;height:560px;border-radius:50%;background:rgba(255,255,255,.08)"></div>
<div style="position:absolute;left:-220px;bottom:80px;width:520px;height:520px;border-radius:50%;background:rgba(0,0,0,.10)"></div>
<div style="position:absolute;left:60px;right:60px;top:56px;display:flex;justify-content:space-between;align-items:center">
 {logo_html}<span style="background:#fff;color:{c1};padding:12px 30px;border-radius:34px;font-size:36px;font-weight:700">ताज़ा खबर</span></div>
<div style="position:absolute;left:60px;right:60px;top:300px;bottom:200px;display:flex;align-items:center">
 <div style="width:14px;align-self:stretch;background:#fff;border-radius:7px;margin-right:36px"></div>
 <div style="font-size:82px;line-height:1.3;font-weight:800;text-shadow:0 4px 14px rgba(0,0,0,.45)">{headline}</div></div>
<div style="position:absolute;left:0;right:0;bottom:0;height:110px;background:rgba(0,0,0,.45);display:flex;justify-content:space-between;align-items:center;padding:0 60px;font-size:32px">
 <b>{page}</b><span style="opacity:.9">Source: {src}</span></div>
</div></body></html>"""

HTML_PHOTO = """<html><body style="margin:0"><div style="width:1080px;height:1350px;position:relative;overflow:hidden;background:#000;font-family:'Noto Sans Devanagari','Hind','Nirmala UI',sans-serif;color:#fff">
<img src="{img}" style="position:absolute;left:-80px;top:-80px;width:1240px;height:1510px;object-fit:cover;filter:blur(40px) brightness(.5)">
<img src="{img}" style="position:absolute;left:0;top:190px;width:1080px;height:700px;object-fit:contain">
<div style="position:absolute;left:60px;right:60px;top:56px;display:flex;justify-content:space-between;align-items:center">
 {logo_html}<span style="background:{c1};color:#fff;padding:12px 30px;border-radius:34px;font-size:36px;font-weight:700">ताज़ा खबर</span></div>
<div style="position:absolute;left:50px;right:50px;bottom:150px;background:rgba(0,0,0,.72);border-left:16px solid {c2};border-radius:22px;padding:34px 40px;box-shadow:0 10px 40px rgba(0,0,0,.5)">
 <div style="font-size:70px;line-height:1.3;font-weight:800">{headline}</div></div>
<div style="position:absolute;left:0;right:0;bottom:0;height:110px;background:rgba(0,0,0,.55);display:flex;justify-content:space-between;align-items:center;padding:0 60px;font-size:32px">
 <b>{page}</b><span style="opacity:.9">Source: {src}</span></div>
</div></body></html>"""

def find_logo():
    if LOGO.exists(): return LOGO
    for f in HERE.glob("*.png"):
        if "logo" in f.name.lower(): return f
    return None

def make_poster(headline, src, path, img_uri=None):
    c1, c2 = random.choice(THEMES)
    lf = find_logo()
    if lf:
        data = base64.b64encode(lf.read_bytes()).decode()
        logo_html = f'<div style="background:#fff;border-radius:24px;padding:12px 18px;display:flex;align-items:center"><img src="data:image/png;base64,{data}" style="height:110px;max-width:340px;object-fit:contain"></div>'
    else:
        print("WARNING: logo.png nahi mili - logo ke bina poster ban raha hai"); logo_html = "<span></span>"
    if img_uri:
        html = HTML_PHOTO.format(c1=c1, c2=c2, img=img_uri, logo_html=logo_html, headline=headline, page=PAGE_NAME, src=src)
    else:
        html = HTML.format(c1=c1, c2=c2, logo_html=logo_html, headline=headline, page=PAGE_NAME, src=src)
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1080, "height": 1350})
        pg.set_content(html); pg.wait_for_timeout(800); pg.screenshot(path=str(path)); b.close()

# ---------- 4. Facebook post ----------
def post_fb(poster, caption):
    r = requests.post(f"https://graph.facebook.com/v21.0/{FB_PAGE_ID}/photos",
        data={"caption": caption, "access_token": FB_TOKEN}, files={"source": open(poster, "rb")}, timeout=60)
    r.raise_for_status(); return r.json()

# ---------- main ----------
# ---------- 1b. Poore internet se khabrein dhundho ----------
def rss_items(url):
    r = requests.get(url, headers=UA, timeout=20); r.raise_for_status()
    return ET.fromstring(r.content).findall(".//item")

def google_news(q):
    url = "https://news.google.com/rss/search?q=" + quote(q + " when:1d") + "&hl=hi&gl=IN&ceid=IN:hi"
    out = []
    for it in rss_items(url):
        src = it.find("source")
        out.append({"url": (it.findtext("link") or "").strip(), "src": (src.text if src is not None and src.text else "News"),
                    "published": parse_dt(it.findtext("pubDate"))})
    return out

def bing_news(q):
    url = "https://www.bing.com/news/search?q=" + quote(q) + "&format=rss&setlang=hi-IN&cc=IN"
    out = []
    for it in rss_items(url):
        link = (it.findtext("link") or "").strip()
        real = parse_qs(urlparse(link).query).get("url", [link])[0]
        out.append({"url": real, "src": urlparse(real).netloc.replace("www.", "") or "News",
                    "published": parse_dt(it.findtext("pubDate"))})
    return out

def site_links():
    out = []
    for name, url in SOURCES.items():
        try: out += [{"url": l, "src": name, "published": None} for l in listing_links(url)]
        except Exception as e: print(f"[{name}] listing fail: {e}")
    return out

def collect_candidates():
    raw = site_links()
    for q in QUERIES:
        for fn in (google_news, bing_news):
            try: raw += fn(q)
            except Exception as e: print(f"{fn.__name__}({q}) fail: {e}")
    now, seen_urls, out = datetime.now(IST), set(), []
    for c in raw:
        u = c["url"]
        if not u or u in seen_urls or seen(u): continue
        seen_urls.add(u)
        p = c["published"]
        if p and (now - p).total_seconds() > MAX_AGE_H * 3600:      # purani khabar
            mark(u); continue
        out.append(c)
    dated = sorted([c for c in out if c["published"]], key=lambda c: c["published"], reverse=True)   # sabse nayi pehle
    undated = [c for c in out if not c["published"]]; random.shuffle(undated)
    return dated + undated

def resolve(c):
    u = c["url"]
    if "news.google.com" not in u: return u
    try:
        from googlenewsdecoder import gnewsdecoder
        r = gnewsdecoder(u, interval=1)
        if r.get("status"): return r["decoded_url"]
    except Exception as e:
        print("google link decode fail:", e)
    return None

def main():
    cands = collect_candidates()
    print(f"{len(cands)} nayi candidate khabrein mili")
    posted = tried = 0
    for c in cands:
        if posted >= MAX_POSTS or tried >= MAX_TRIES: break
        try:
            link = resolve(c)
            if not link or seen(link): mark(c["url"]); continue
            facts = article_facts(link)
            pub = c["published"] or parse_dt(facts["published"])
            if pub and (datetime.now(IST) - pub).total_seconds() > MAX_AGE_H * 3600:
                print("purani khabar skip:", link); mark(c["url"]); mark(link); continue
            if not pub and STRICT_DATE:
                print("date pata nahi, skip:", link); mark(c["url"]); mark(link); continue
            facts["published"] = pub.astimezone(IST).strftime("%d %b %Y %H:%M IST") if pub else "pata nahi"
            tried += 1
            src = c["src"]; cont = make_content(src, facts)
            mark(c["url"]); mark(link)
            if cont.get("skip"): print("skip:", link); continue
            caption = cont["caption"] + "\n\n" + " ".join(cont["hashtags"]) + f"\n\nSource: {src}"
            img_uri = fetch_image_uri(facts["image"]) if (USE_IMG and facts.get("image")) else None
            poster = OUT / f"{int(time.time())}.png"; make_poster(cont["headline"], src, poster, img_uri)
            print("photo:", "haan" if img_uri else "nahi (gradient poster)")
            (poster.with_suffix(".txt")).write_text(caption + "\n\n" + link, encoding="utf-8")
            if DRY_RUN: print("DRY RUN saved:", poster)
            else: print("posted:", post_fb(poster, caption))
            save_headline(cont["headline"])
            posted += 1; time.sleep(random.randint(5, 20))
        except RateLimited:
            print("Gemini limit lag gayi. Run band kar raha hu - agle run me dobara try hoga.")
            return
        except Exception as e:
            print(f"error {c['url']}: {e}"); mark(c["url"])
    print(f"khatam: {posted} post, {tried} articles Gemini ko dikhaye")

if __name__ == "__main__":
    main()
