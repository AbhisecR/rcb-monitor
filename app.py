"""
RCB Ticket Monitor — Render deployment
Alerts: Telegram + Gmail
4 monitoring layers: API, DOM, Keyword, Social
"""

import os
import sys
import time
import hashlib
import smtplib
import threading
import requests
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from flask import Flask, render_template, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("RCB")

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG — all values come from Render environment variables
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "935393436")

EMAIL_SENDER     = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD   = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECEIVER   = os.environ.get("EMAIL_RECEIVER", "")

RCB_URLS = [
    os.environ.get("RCB_URL_1", "https://rcb.iplt20.com/"),
    os.environ.get("RCB_URL_2", "https://rcb.iplt20.com/matches"),
    "https://www.bookmyshow.com/sports/royal-challengers-bengaluru/ET00390804",
]

TICKET_KEYWORDS = [
    "buy now", "book ticket", "book now", "tickets available",
    "buy ticket", "tickets on sale", "get tickets", "book your",
    "2300", "1000", "sold out",
]

TWITTER_HANDLES  = ["RCBTweets", "rcbofficial"]
NITTER_MIRRORS   = [
    "https://nitter.privacydev.net",
    "https://nitter.net",
    "https://nitter.it",
    "https://nitter.1d4.us",
]

INTERVAL_API     = 5
INTERVAL_DOM     = 60
INTERVAL_KEYWORD = 60
INTERVAL_SOCIAL  = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept":     "text/html,application/xhtml+xml,application/json,*/*;q=0.9",
}

# ─────────────────────────────────────────────────────────────────────────────
#  SHARED STATE
# ─────────────────────────────────────────────────────────────────────────────

state = {
    "layers": {
        "api":     {"status": "watching", "last_check": None, "checks": 0, "alerts": 0},
        "dom":     {"status": "watching", "last_check": None, "checks": 0, "alerts": 0},
        "keyword": {"status": "watching", "last_check": None, "checks": 0, "alerts": 0},
        "social":  {"status": "watching", "last_check": None, "checks": 0, "alerts": 0},
    },
    "alerts":     [],
    "monitoring": True,
}
state_lock   = threading.Lock()
fired_alerts = set()
alert_lock   = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
#  ALERT CHANNELS
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message):
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set — skipping")
        return
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        text = (
            f"🚨 *RCB TICKET ALERT*\n\n"
            f"{message}\n\n"
            f"👉 [Buy Tickets Now](https://rcb.iplt20.com/)\n"
            f"🕐 {datetime.now().strftime('%d %b %H:%M:%S')}"
        )
        resp = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "Markdown",
        }, timeout=10)
        if resp.status_code == 200:
            log.info("✅ Telegram sent")
        else:
            log.error(f"Telegram error: {resp.text}")
    except Exception as e:
        log.error(f"Telegram failed: {e}")


def send_email(subject, body):
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        log.warning("Email not configured — skipping")
        return
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = EMAIL_RECEIVER or EMAIL_SENDER
        html = f"""
        <html><body style="font-family:sans-serif;background:#0a0a0a;color:#f0f0f0;padding:32px;">
        <div style="max-width:520px;margin:0 auto;background:#161616;border-radius:12px;
                    padding:28px;border:2px solid #E8001C;">
          <div style="background:#E8001C;color:white;font-size:22px;font-weight:800;
                      padding:12px 20px;border-radius:8px;margin-bottom:20px;">
            🚨 RCB TICKET ALERT
          </div>
          <p style="font-size:16px;margin-bottom:20px;white-space:pre-wrap;line-height:1.6">{body}</p>
          <a href="https://rcb.iplt20.com/"
             style="display:inline-block;background:#E8001C;color:white;padding:14px 28px;
                    border-radius:8px;text-decoration:none;font-weight:800;font-size:18px;">
            BUY TICKETS NOW →
          </a>
          <p style="color:#555;font-size:12px;margin-top:24px;">
            Sent by RCB Monitor at {datetime.now().strftime('%d %b %Y %H:%M:%S')}
          </p>
        </div>
        </body></html>
        """
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html,  "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER or EMAIL_SENDER, msg.as_string())
        log.info("✅ Email sent")
    except Exception as e:
        log.error(f"Email failed: {e}")


def fire_alert(layer, message, dedupe_key=None):
    key = dedupe_key or f"{layer}:{message[:50]}"
    with alert_lock:
        if key in fired_alerts:
            return
        fired_alerts.add(key)

    timestamp = datetime.now().strftime("%d %b %H:%M:%S")
    full_msg  = f"[{layer.upper()}] {message}"

    log.warning("=" * 60)
    log.warning(f"🚨 ALERT: {full_msg}")
    log.warning("=" * 60)

    with state_lock:
        state["alerts"].insert(0, {
            "layer":   layer,
            "message": message,
            "time":    timestamp,
        })
        state["layers"][layer]["alerts"] += 1
        if len(state["alerts"]) > 100:
            state["alerts"] = state["alerts"][:100]

    subject = f"🚨 RCB TICKET ALERT [{layer.upper()}] — {timestamp}"
    threads = [
        threading.Thread(target=send_telegram, args=(full_msg,),                 daemon=True),
        threading.Thread(target=send_email,    args=(subject, full_msg),          daemon=True),
    ]
    for t in threads:
        t.start()


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def make_hash(content):
    return hashlib.md5(str(content).encode("utf-8", errors="ignore")).hexdigest()

def fetch(url, timeout=12):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        return r.text
    except Exception as e:
        log.debug(f"Fetch error [{url}]: {e}")
        return None

def has_ticket_keyword(text):
    t = text.lower()
    return [kw for kw in TICKET_KEYWORDS if kw in t]

def update_layer(layer, status="watching"):
    with state_lock:
        state["layers"][layer]["last_check"] = datetime.now().strftime("%H:%M:%S")
        state["layers"][layer]["checks"]    += 1
        state["layers"][layer]["status"]     = status


# ─────────────────────────────────────────────────────────────────────────────
#  LAYER 1 — API monitor  (every 5 sec)
# ─────────────────────────────────────────────────────────────────────────────

def layer1_api():
    log.info("Layer 1 (API) started — every %ds", INTERVAL_API)
    hashes = {}
    while True:
        if not state["monitoring"]:
            time.sleep(5); continue
        for url in RCB_URLS:
            html = fetch(url)
            if html is None:
                update_layer("api", "error"); continue
            h    = make_hash(html)
            prev = hashes.get(url)
            if prev and h != prev:
                kws = has_ticket_keyword(html)
                if kws:
                    fire_alert("api",
                        f"Page changed AND ticket keywords found: {kws}\nURL: {url}",
                        dedupe_key=f"api_kw_{url}")
                else:
                    fire_alert("api",
                        f"Page content changed — check manually!\nURL: {url}",
                        dedupe_key=f"api_ch_{url}_{h[:8]}")
            hashes[url] = h
            update_layer("api")
        time.sleep(INTERVAL_API)


# ─────────────────────────────────────────────────────────────────────────────
#  LAYER 2 — DOM structure monitor  (every 60 sec)
# ─────────────────────────────────────────────────────────────────────────────

def layer2_dom():
    log.info("Layer 2 (DOM) started — every %ds", INTERVAL_DOM)
    hashes = {}
    while True:
        if not state["monitoring"]:
            time.sleep(30); continue
        for url in RCB_URLS:
            html = fetch(url)
            if html is None:
                update_layer("dom", "error"); continue
            soup     = BeautifulSoup(html, "html.parser")
            elements = []
            for tag in soup.find_all(["a", "button", "h1", "h2", "h3", "input", "form"]):
                text = tag.get_text(strip=True)[:80]
                href = tag.get("href", "")[:60]
                if text or href:
                    elements.append(f"{tag.name}|{text}|{href}")
            h    = make_hash("\n".join(sorted(elements)))
            prev = hashes.get(url)
            if prev and h != prev:
                fire_alert("dom",
                    f"Page layout changed — new buttons or sections appeared!\nURL: {url}",
                    dedupe_key=f"dom_{url}_{h[:8]}")
            hashes[url] = h
            update_layer("dom")
        time.sleep(INTERVAL_DOM)


# ─────────────────────────────────────────────────────────────────────────────
#  LAYER 3 — Keyword scanner  (every 60 sec)
# ─────────────────────────────────────────────────────────────────────────────

def layer3_keywords():
    log.info("Layer 3 (Keyword) started — every %ds", INTERVAL_KEYWORD)
    seen = set()
    while True:
        if not state["monitoring"]:
            time.sleep(30); continue
        for url in RCB_URLS:
            html = fetch(url)
            if html is None:
                update_layer("keyword", "error"); continue
            kws = has_ticket_keyword(html)
            new = [kw for kw in kws if f"{url}:{kw}" not in seen]
            if new:
                for kw in new:
                    seen.add(f"{url}:{kw}")
                fire_alert("keyword",
                    f"Ticket keywords found on page!\nKeywords: {new}\nURL: {url}",
                    dedupe_key=f"kw_{'_'.join(new)}_{url}")
            update_layer("keyword")
        time.sleep(INTERVAL_KEYWORD)


# ─────────────────────────────────────────────────────────────────────────────
#  LAYER 4 — Social monitor  (every 30 sec)
# ─────────────────────────────────────────────────────────────────────────────

def layer4_social():
    log.info("Layer 4 (Social) started — every %ds", INTERVAL_SOCIAL)
    seen_tweets = set()
    mirror_idx  = 0
    while True:
        if not state["monitoring"]:
            time.sleep(30); continue
        for handle in TWITTER_HANDLES:
            mirror = NITTER_MIRRORS[mirror_idx % len(NITTER_MIRRORS)]
            mirror_idx += 1
            html = fetch(f"{mirror}/{handle}", timeout=10)
            if not html:
                update_layer("social", "error"); continue
            soup   = BeautifulSoup(html, "html.parser")
            tweets = soup.find_all("div", class_="tweet-content")
            for tweet in tweets[:15]:
                text = tweet.get_text(strip=True)
                tkey = make_hash(text)
                if tkey in seen_tweets:
                    continue
                seen_tweets.add(tkey)
                if any(kw in text.lower() for kw in
                       ["ticket", "book", "sale", "buy", "available", "chinnaswamy"]):
                    fire_alert("social",
                        f"@{handle} tweeted about tickets:\n\"{text[:200]}\"",
                        dedupe_key=f"tw_{tkey}")
            update_layer("social")
        time.sleep(INTERVAL_SOCIAL)


# ─────────────────────────────────────────────────────────────────────────────
#  STARTUP TEST — sends Telegram + Email to confirm everything works
# ─────────────────────────────────────────────────────────────────────────────

def startup_test():
    time.sleep(3)  # wait for server to be ready
    log.info("Running startup test...")

    msg = (
        "✅ RCB Monitor is live on Render!\n\n"
        "All 4 layers are watching:\n"
        "• Layer 1 — API (every 5s)\n"
        "• Layer 2 — DOM (every 60s)\n"
        "• Layer 3 — Keywords (every 60s)\n"
        "• Layer 4 — Social (every 30s)\n\n"
        "You will be the first to know when tickets drop 🏏"
    )

    send_telegram(msg)
    send_email("✅ RCB Monitor Live on Render", msg)
    log.info("Startup test complete.")


# ─────────────────────────────────────────────────────────────────────────────
#  FLASK ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/state")
def get_state():
    with state_lock:
        return jsonify(state)

@app.route("/api/toggle", methods=["POST"])
def toggle():
    with state_lock:
        state["monitoring"] = not state["monitoring"]
        return jsonify({"monitoring": state["monitoring"]})

@app.route("/api/test", methods=["POST"])
def test_alert():
    fire_alert("test", "This is a test alert — Telegram and Gmail are working!")
    return jsonify({"ok": True})

@app.route("/api/config", methods=["POST"])
def update_config():
    data = request.json or {}
    if data.get("url1"): RCB_URLS[0] = data["url1"]
    if data.get("url2"): RCB_URLS[1] = data["url2"]
    return jsonify({"ok": True})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


# ─────────────────────────────────────────────────────────────────────────────
#  START
# ─────────────────────────────────────────────────────────────────────────────

def start_monitors():
    monitors = [layer1_api, layer2_dom, layer3_keywords, layer4_social, startup_test]
    for fn in monitors:
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        time.sleep(0.3)

start_monitors()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
