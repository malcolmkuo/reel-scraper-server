import os
import re
import glob
import json
import uuid
import tempfile
import requests
import boto3
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
from curl_cffi import requests as cffi_requests

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN")
D1_DATABASE_ID = os.environ.get("D1_DATABASE_ID")

R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "reel-scraper-videos")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")

TEAM_PASSWORD = os.environ.get("TEAM_PASSWORD", "malithegoat123")

# --- COOKIES ---
# Write cookie files from env vars once at startup so yt-dlp can use them.
YOUTUBE_COOKIE_FILE = None
INSTAGRAM_COOKIE_FILE = None
TIKTOK_COOKIE_FILE = None

def _write_cookie_file(env_var, path):
    content = os.environ.get(env_var, "").strip()
    if content:
        with open(path, "w") as f:
            f.write(content)
        return path
    return None

YOUTUBE_COOKIE_FILE   = _write_cookie_file("YOUTUBE_COOKIES",   "/tmp/youtube_cookies.txt")
INSTAGRAM_COOKIE_FILE = _write_cookie_file("INSTAGRAM_COOKIES", "/tmp/instagram_cookies.txt")
TIKTOK_COOKIE_FILE    = _write_cookie_file("TIKTOK_COOKIES",    "/tmp/tiktok_cookies.txt")


# --- INSTAGRAM DIRECT SCRAPER (bypasses yt-dlp) ---

# Known doc_ids for Instagram's GraphQL endpoint.  Instagram rotates these
# every few weeks.  We try each one in order until one works.  To update,
# set the IG_DOC_ID env var on Render — it will be tried first.
_GRAPHQL_DOC_IDS = [
    os.environ.get("IG_DOC_ID", ""),       # custom override (tried first)
    "10015901848480474",
    "8845758582119845",
    "17991233890457762",
]
GRAPHQL_DOC_IDS = [d for d in _GRAPHQL_DOC_IDS if d]

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

def _extract_shortcode(url):
    """Pull the shortcode from any Instagram reel/post URL."""
    m = re.search(r'instagram\.com/(?:reels?|p)/([A-Za-z0-9_-]+)', url)
    return m.group(1) if m else None


def _ig_graphql_fetch(shortcode):
    """Hit Instagram's GraphQL API directly via curl_cffi (Chrome TLS fingerprint).
    No cookies or login required.  Returns the media dict or raises."""
    headers = {
        "User-Agent": CHROME_UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-IG-App-ID": "936619743392459",
        "X-FB-LSD": "AVqbxe3J_YA",
        "X-ASBD-ID": "129477",
        "Sec-Fetch-Site": "same-origin",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
    }
    variables = json.dumps({"shortcode": shortcode}, separators=(",", ":"))

    last_err = None
    for doc_id in GRAPHQL_DOC_IDS:
        try:
            data = f"variables={variables}&doc_id={doc_id}&lsd=AVqbxe3J_YA"
            resp = cffi_requests.post(
                "https://www.instagram.com/api/graphql",
                headers=headers,
                data=data,
                impersonate="chrome131",
                timeout=20,
            )
            if resp.status_code != 200:
                last_err = f"GraphQL returned {resp.status_code}"
                continue
            body = resp.json()
            media = (body.get("data", {}).get("xdt_shortcode_media")
                     or body.get("data", {}).get("shortcode_media"))
            if media and media.get("video_url"):
                return media
            last_err = "GraphQL returned no video data"
        except Exception as e:
            last_err = str(e)
            continue

    raise Exception(f"Instagram GraphQL failed: {last_err}")


def _ig_api_fallback(shortcode):
    """Fallback: try the ?__a=1&__d=dis JSON endpoint (needs cookies)."""
    if not INSTAGRAM_COOKIE_FILE:
        raise Exception("No Instagram cookies configured for fallback")

    with open(INSTAGRAM_COOKIE_FILE) as f:
        cookie_text = f.read()

    # Parse Netscape cookie file into a dict
    cookies = {}
    for line in cookie_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]

    headers = {
        "User-Agent": CHROME_UA,
        "X-IG-App-ID": "936619743392459",
        "Referer": "https://www.instagram.com/",
    }

    resp = cffi_requests.get(
        f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis",
        headers=headers,
        cookies=cookies,
        impersonate="chrome131",
        timeout=20,
    )
    if resp.status_code != 200:
        raise Exception(f"Instagram API returned {resp.status_code}")

    body = resp.json()
    items = body.get("items") or body.get("graphql", {}).get("shortcode_media")
    if isinstance(items, list) and items:
        return items[0]
    if isinstance(items, dict):
        return items
    raise Exception("Instagram API returned no media data")


def fetch_instagram_reel(url):
    """Fetch Instagram reel metadata + direct video URL.
    Layer 1: GraphQL API (no auth)  →  Layer 2: ?__a=1 endpoint (cookies)
    Returns a normalized info dict matching the shape the rest of add_reel expects."""
    shortcode = _extract_shortcode(url)
    if not shortcode:
        raise Exception("Could not extract shortcode from Instagram URL")

    media = None
    errors = []

    # Layer 1: GraphQL (no auth needed)
    try:
        media = _ig_graphql_fetch(shortcode)
    except Exception as e:
        errors.append(f"GraphQL: {e}")

    # Layer 2: ?__a=1 fallback (uses cookies if available)
    if not media:
        try:
            media = _ig_api_fallback(shortcode)
        except Exception as e:
            errors.append(f"API fallback: {e}")

    if not media:
        raise Exception(
            "All Instagram extraction methods failed. "
            + " | ".join(errors)
        )

    # Normalize into the shape add_reel expects
    video_url = media.get("video_url", "")
    if not video_url:
        # Try video_versions array (private API format)
        versions = media.get("video_versions", [])
        if versions:
            video_url = versions[0].get("url", "")

    if not video_url:
        raise Exception("Instagram returned media but no video URL — post may be an image")

    # Extract caption
    caption = ""
    edges = media.get("edge_media_to_caption", {}).get("edges", [])
    if edges:
        caption = edges[0].get("node", {}).get("text", "")
    elif media.get("caption"):
        cap = media["caption"]
        caption = cap.get("text", "") if isinstance(cap, dict) else str(cap)

    # Extract uploader
    owner = media.get("owner", {})
    uploader = owner.get("username") or owner.get("full_name") or "Unknown"
    uploader_id = owner.get("id", "")
    uploader_url = f"https://www.instagram.com/{uploader}/" if uploader != "Unknown" else ""

    # Dimensions
    dims = media.get("dimensions", {})
    width = int(dims.get("width", 0) or media.get("original_width", 0) or 0)
    height = int(dims.get("height", 0) or media.get("original_height", 0) or 0)

    # Thumbnail
    thumbnail = (media.get("display_url")
                 or media.get("thumbnail_src")
                 or media.get("image_versions2", {}).get("candidates", [{}])[0].get("url", ""))

    duration = 0
    raw_dur = media.get("video_duration")
    if raw_dur:
        duration = int(float(raw_dur))

    return {
        "video_download_url": video_url,
        "title": (caption[:80] + "...") if len(caption) > 80 else caption or "Untitled Reel",
        "id": shortcode,
        "uploader": uploader,
        "uploader_id": uploader_id,
        "uploader_url": uploader_url,
        "duration": duration,
        "description": caption,
        "upload_date": "",
        "track": "Original Audio",
        "tags": [],
        "thumbnail": thumbnail,
        "channel_follower_count": int(owner.get("edge_followed_by", {}).get("count", 0) or 0),
        "width": width,
        "height": height,
        "categories": [],
        "like_count": int(media.get("edge_media_preview_like", {}).get("count", 0)
                         or media.get("like_count", 0) or 0),
        "view_count": int(media.get("video_play_count", 0)
                         or media.get("video_view_count", 0)
                         or media.get("play_count", 0) or 0),
        "comment_count": int(media.get("edge_media_to_parent_comment", {}).get("count", 0)
                             or media.get("comment_count", 0) or 0),
        "repost_count": 0,
    }


# --- YT-DLP OPTIONS (YouTube + TikTok) ---

def get_ydl_opts(platform, extra=None):
    """Return yt-dlp options with platform-appropriate cookies and headers injected."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "no_check_certificates": True,
        "extractor_retries": 3,
        "socket_timeout": 30,
    }

    if platform == "youtube":
        if YOUTUBE_COOKIE_FILE:
            opts["cookiefile"] = YOUTUBE_COOKIE_FILE

    elif platform == "tiktok":
        opts["http_headers"] = {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            "Referer": "https://www.tiktok.com/",
        }
        if TIKTOK_COOKIE_FILE:
            opts["cookiefile"] = TIKTOK_COOKIE_FILE

    if extra:
        opts.update(extra)
    return opts

# --- D1 HELPER ---
D1_BASE_URL = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/d1/database/{D1_DATABASE_ID}/query"

def d1_query(sql, params=None):
    """Execute a SQL query against Cloudflare D1 via REST API."""
    body = {"sql": sql}
    if params:
        body["params"] = params
    resp = requests.post(
        D1_BASE_URL,
        headers={
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json=body,
    )
    data = resp.json()
    if not data.get("success"):
        errors = data.get("errors", [])
        raise Exception(f"D1 query failed: {errors}")
    return data["result"][0]


# --- R2 HELPERS ---
s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
)


def upload_to_r2(local_path, key, content_type="video/mp4"):
    """Upload a file to R2 and return its public URL."""
    s3.upload_file(local_path, R2_BUCKET_NAME, key, ExtraArgs={"ContentType": content_type})
    return f"{R2_PUBLIC_URL}/{key}"


def delete_from_r2(key):
    """Delete a file from R2."""
    s3.delete_object(Bucket=R2_BUCKET_NAME, Key=key)


# --- INIT DB ---
def init_db():
    """Create the reels table if it doesn't exist."""
    d1_query("""
        CREATE TABLE IF NOT EXISTS reels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            video_url TEXT,
            title TEXT,
            added_by TEXT DEFAULT 'Anonymous',
            language TEXT DEFAULT 'English',
            description TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            duration INTEGER DEFAULT 0,
            uploader TEXT DEFAULT 'Unknown',
            upload_date TEXT DEFAULT '',
            audio TEXT DEFAULT 'Original Audio',
            likes INTEGER DEFAULT 0,
            views INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            shares INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # Add new columns (safe to call repeatedly)
    new_columns = [
        ("thumbnail_url", "TEXT DEFAULT ''"),
        ("uploader_id", "TEXT DEFAULT ''"),
        ("uploader_url", "TEXT DEFAULT ''"),
        ("channel_follower_count", "INTEGER DEFAULT 0"),
        ("width", "INTEGER DEFAULT 0"),
        ("height", "INTEGER DEFAULT 0"),
        ("aspect_ratio", "REAL DEFAULT 0"),
        ("categories", "TEXT DEFAULT ''"),
        ("platform", "TEXT DEFAULT ''"),
    ]
    for col_name, col_type in new_columns:
        try:
            d1_query(f"ALTER TABLE reels ADD COLUMN {col_name} {col_type}")
        except Exception:
            pass  # Column already exists


def sanitize_filename(name):
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    return clean.replace(" ", "_")


# --- ROUTES ---
@app.route("/")
def home():
    return "Reel Vault Engine Online"


@app.route("/login", methods=["POST"])
def login():
    if request.json.get("password") == TEAM_PASSWORD:
        return jsonify({"status": "success"}), 200
    return jsonify({"error": "Unauthorized"}), 401


@app.route("/cookie_status", methods=["GET"])
def cookie_status():
    """Check which platform cookies are configured."""
    return jsonify({
        "youtube": YOUTUBE_COOKIE_FILE is not None,
        "instagram": INSTAGRAM_COOKIE_FILE is not None,
        "tiktok": TIKTOK_COOKIE_FILE is not None,
    })


@app.route("/add_reel", methods=["POST"])
def add_reel():
    data = request.json
    url = data.get("url")
    username = data.get("username", "Anonymous")
    language = data.get("language", "English")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        # 1. Check Duplicates
        existing = d1_query("SELECT id FROM reels WHERE url = ?", [url])
        if existing.get("results"):
            return jsonify({"status": "exists", "message": "Reel already in vault"}), 200

        # Detect platform
        if "tiktok" in url:
            platform_early = "tiktok"
        elif "youtube" in url or "youtu.be" in url:
            platform_early = "youtube"
        else:
            platform_early = "instagram"

        # --- INSTAGRAM: direct GraphQL scraper (no yt-dlp) ---
        if platform_early == "instagram":
            info = fetch_instagram_reel(url)
            video_download_url = info["video_download_url"]
            video_id = info["id"]
            r2_key = f"instagram_{video_id}.mp4"
            local_path = f"/tmp/{r2_key}"

            # Download video directly from Instagram CDN via curl_cffi
            vid_resp = cffi_requests.get(
                video_download_url,
                headers={"User-Agent": CHROME_UA, "Referer": "https://www.instagram.com/"},
                impersonate="chrome131",
                timeout=60,
            )
            if vid_resp.status_code != 200:
                raise Exception(f"Failed to download video: HTTP {vid_resp.status_code}")
            with open(local_path, "wb") as f:
                f.write(vid_resp.content)

            if os.path.getsize(local_path) < 1000:
                os.remove(local_path)
                raise Exception("Downloaded file is too small — Instagram may have blocked the request")

            # Upload video to R2
            public_url = upload_to_r2(local_path, r2_key)

            # Download & upload thumbnail
            thumbnail_url = ""
            thumbnail = info.get("thumbnail", "")
            if thumbnail:
                try:
                    thumb_resp = cffi_requests.get(
                        thumbnail,
                        headers={"User-Agent": CHROME_UA},
                        impersonate="chrome131",
                        timeout=10,
                    )
                    if thumb_resp.status_code == 200:
                        thumb_path = f"/tmp/instagram_{video_id}_thumb.jpg"
                        with open(thumb_path, "wb") as f:
                            f.write(thumb_resp.content)
                        thumbnail_url = upload_to_r2(thumb_path, f"thumbs/instagram_{video_id}.jpg", content_type="image/jpeg")
                        if os.path.exists(thumb_path):
                            os.remove(thumb_path)
                except Exception:
                    thumbnail_url = ""

            # Metadata
            uploader = info.get("uploader", "Unknown")
            duration = info.get("duration", 0)
            description = info.get("description", "")
            tags = info.get("tags", [])
            tags_str = ", ".join(tags) if tags else ""
            width = info.get("width", 0)
            height = info.get("height", 0)
            aspect_ratio = round(width / height, 4) if height else 0

            # Insert into D1
            d1_query(
                """INSERT INTO reels (url, video_url, title, added_by, language, description, tags, duration, uploader, upload_date, audio, likes, views, comments, shares, thumbnail_url, uploader_id, uploader_url, channel_follower_count, width, height, aspect_ratio, categories, platform)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    url,
                    public_url,
                    info.get("title", "Untitled Reel"),
                    username,
                    language,
                    description,
                    tags_str,
                    duration,
                    uploader,
                    info.get("upload_date", ""),
                    info.get("track", "Original Audio"),
                    int(info.get("like_count", 0) or 0),
                    int(info.get("view_count", 0) or 0),
                    int(info.get("comment_count", 0) or 0),
                    int(info.get("repost_count", 0) or 0),
                    thumbnail_url,
                    info.get("uploader_id", ""),
                    info.get("uploader_url", ""),
                    int(info.get("channel_follower_count", 0) or 0),
                    width,
                    height,
                    aspect_ratio,
                    ", ".join(info.get("categories", []) or []),
                    "instagram",
                ],
            )

            if os.path.exists(local_path):
                os.remove(local_path)
            return jsonify({"status": "success", "file": r2_key})

        # --- YOUTUBE / TIKTOK: use yt-dlp ---
        # 2. Extract Metadata
        with yt_dlp.YoutubeDL(get_ydl_opts(platform_early)) as ydl:
            info = ydl.extract_info(url, download=False)

            actual_title = info.get("title", "Untitled_Reel")
            video_id = info.get("id") or uuid.uuid4().hex[:12]
            r2_key = f"{platform_early}_{video_id}.mp4"
            local_path = f"/tmp/{r2_key}"

            uploader = info.get("uploader") or info.get("channel") or "Unknown"
            raw_duration = info.get("duration", 0)
            duration = int(float(raw_duration)) if raw_duration else 0
            description = info.get("description", "") or ""
            upload_date = info.get("upload_date", "")
            audio_track = info.get("track") or info.get("artist") or "Original Audio"
            tags = info.get("tags", [])
            tags_str = ", ".join(tags) if tags else ""

            thumbnail = info.get("thumbnail", "")
            uploader_id = info.get("uploader_id", "") or info.get("channel_id", "") or ""
            uploader_url = info.get("uploader_url", "") or info.get("channel_url", "") or ""
            channel_follower_count = int(info.get("channel_follower_count", 0) or 0)
            width = int(info.get("width", 0) or 0)
            height = int(info.get("height", 0) or 0)
            aspect_ratio = round(width / height, 4) if height else 0
            categories = ", ".join(info.get("categories", []) or [])
            platform = platform_early

        # 3. Download video
        download_extra = {
            "outtmpl": local_path,
            "format": (
                "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]"
                "/bestvideo[ext=mp4]+bestaudio"
                "/best[ext=mp4]"
                "/best"
            ),
            "merge_output_format": "mp4",
            "postprocessors": [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }],
        }
        with yt_dlp.YoutubeDL(get_ydl_opts(platform_early, download_extra)) as ydl:
            ydl.download([url])

        if not os.path.exists(local_path):
            candidates = glob.glob(f"/tmp/{platform_early}_{video_id}.*")
            if candidates:
                local_path = candidates[0]
            else:
                raise Exception("Download completed but output file not found")

        # 4. Upload video to R2
        public_url = upload_to_r2(local_path, r2_key)

        # 5. Download & upload thumbnail to R2
        thumbnail_url = ""
        if thumbnail:
            try:
                thumb_resp = requests.get(thumbnail, timeout=10)
                if thumb_resp.status_code == 200:
                    thumb_path = os.path.join(tempfile.gettempdir(), f"{platform_early}_{video_id}_thumb.jpg")
                    with open(thumb_path, "wb") as f:
                        f.write(thumb_resp.content)
                    thumbnail_url = upload_to_r2(thumb_path, f"thumbs/{platform_early}_{video_id}.jpg", content_type="image/jpeg")
                    if os.path.exists(thumb_path):
                        os.remove(thumb_path)
            except Exception:
                thumbnail_url = ""

        # 6. Insert into D1
        d1_query(
            """INSERT INTO reels (url, video_url, title, added_by, language, description, tags, duration, uploader, upload_date, audio, likes, views, comments, shares, thumbnail_url, uploader_id, uploader_url, channel_follower_count, width, height, aspect_ratio, categories, platform)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                url,
                public_url,
                actual_title,
                username,
                language,
                description,
                tags_str,
                duration,
                uploader,
                upload_date,
                audio_track,
                int(info.get("like_count", 0) or 0),
                int(info.get("view_count", 0) or 0),
                int(info.get("comment_count", 0) or 0),
                int(info.get("repost_count", 0) or 0),
                thumbnail_url,
                uploader_id,
                uploader_url,
                channel_follower_count,
                width,
                height,
                aspect_ratio,
                categories,
                platform,
            ],
        )

        if os.path.exists(local_path):
            os.remove(local_path)
        return jsonify({"status": "success", "file": r2_key})

    except Exception as e:
        err = str(e)
        if "Sign in to confirm" in err or "bot" in err.lower():
            cookie_var = "YOUTUBE_COOKIES" if platform_early == "youtube" else f"{platform_early.upper()}_COOKIES"
            err = f"[{platform_early}] Blocked by bot detection. Set the {cookie_var} env var in Render with exported browser cookies."
        elif "not available" in err.lower() and platform_early == "tiktok":
            err = "TikTok blocked this request. Set the TIKTOK_COOKIES env var in Render with exported browser cookies."
        return jsonify({"error": err}), 500


@app.route("/delete_reel", methods=["POST"])
def delete_reel():
    reel_id = request.json.get("id")
    try:
        record = d1_query("SELECT video_url, thumbnail_url FROM reels WHERE id = ?", [reel_id])
        if record.get("results"):
            row = record["results"][0]
            video_url = row.get("video_url", "")
            thumbnail_url = row.get("thumbnail_url", "")
            if video_url:
                delete_from_r2(video_url.split("/")[-1])
            if thumbnail_url and "/thumbs/" in thumbnail_url:
                delete_from_r2("thumbs/" + thumbnail_url.split("/thumbs/")[-1])

        d1_query("DELETE FROM reels WHERE id = ?", [reel_id])
        return jsonify({"status": "deleted"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/library", methods=["GET"])
def get_library():
    try:
        limit = request.args.get("limit", 20, type=int)
        offset = request.args.get("offset", 0, type=int)
        search = request.args.get("search", "").strip()
        language = request.args.get("language", "").strip()
        platform = request.args.get("platform", "").strip()
        added_by = request.args.get("added_by", "").strip()
        date_range = request.args.get("date_range", "").strip()
        duration_bucket = request.args.get("duration_bucket", "").strip()
        sort = request.args.get("sort", "vault_newest").strip()

        clauses = []
        params = []

        if search:
            clauses.append("(title LIKE ? OR uploader LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if language:
            clauses.append("language = ?")
            params.append(language)
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if added_by:
            clauses.append("added_by = ?")
            params.append(added_by)
        if date_range == "today":
            clauses.append("date(created_at) = date('now')")
        elif date_range == "week":
            clauses.append("created_at >= datetime('now', '-7 days')")
        elif date_range == "month":
            clauses.append("created_at >= datetime('now', '-30 days')")

        if duration_bucket == "short":
            clauses.append("duration < 15")
        elif duration_bucket == "medium":
            clauses.append("duration >= 15 AND duration < 30")
        elif duration_bucket == "long":
            clauses.append("duration >= 30 AND duration < 60")
        elif duration_bucket == "extended":
            clauses.append("duration >= 60")

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        sort_map = {
            "most_liked":     "likes DESC",
            "most_viewed":    "views DESC",
            "most_shares":    "shares DESC",
            "most_comments":  "comments DESC",
            "longest":        "duration DESC",
            "shortest":       "duration ASC",
            "upload_newest":  "upload_date DESC",
            "upload_oldest":  "upload_date ASC",
            "vault_newest":   "id DESC",
            # legacy aliases
            "newest":         "id DESC",
            "oldest":         "id ASC",
        }
        order = sort_map.get(sort, "id DESC")

        # Count matching rows (same WHERE, no LIMIT/OFFSET)
        count_sql = f"SELECT COUNT(*) AS total FROM reels {where}"
        count_result = d1_query(count_sql, params if params else None)
        total = count_result["results"][0]["total"] if count_result.get("results") else 0

        sql = f"SELECT * FROM reels {where} ORDER BY {order} LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        result = d1_query(sql, params)
        return jsonify({"results": result.get("results", []), "total": total})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/stats", methods=["GET"])
def get_stats():
    try:
        total = d1_query("SELECT COUNT(*) as count FROM reels")
        total_count = total["results"][0]["count"] if total.get("results") else 0

        lang = d1_query("SELECT language, COUNT(*) as count FROM reels GROUP BY language ORDER BY count DESC")
        languages = lang.get("results", [])

        agg = d1_query("SELECT COALESCE(SUM(likes),0) as total_likes, COALESCE(SUM(views),0) as total_views, COALESCE(SUM(comments),0) as total_comments, COALESCE(SUM(shares),0) as total_shares FROM reels")
        engagement = agg["results"][0] if agg.get("results") else {}

        plat = d1_query("SELECT platform, COUNT(*) as count FROM reels WHERE platform != '' GROUP BY platform ORDER BY count DESC")
        platforms = plat.get("results", [])

        members = d1_query("SELECT added_by, COUNT(*) as count FROM reels WHERE added_by != '' GROUP BY added_by ORDER BY count DESC")
        team_members = members.get("results", [])

        return jsonify({
            "total": total_count,
            "languages": languages,
            "platforms": platforms,
            "team_members": team_members,
            "engagement": engagement,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- STARTUP ---
with app.app_context():
    try:
        init_db()
        print("D1 table initialized.")
    except Exception as e:
        print(f"Warning: Could not init D1 table on startup: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
