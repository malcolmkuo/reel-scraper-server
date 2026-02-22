import os
import re
import tempfile
import requests
import boto3
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp

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
        # Use the iOS player client — bypasses bot/sign-in checks without needing cookies.
        # Falls back to web_embedded and tv clients if iOS fails.
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["ios", "web_embedded", "tv_embedded"],
                "skip": ["dash", "hls"],
            }
        }
        opts["http_headers"] = {
            "User-Agent": (
                "com.google.ios.youtube/19.29.1 "
                "(iPhone16,2; U; CPU iOS 17_5_1 like Mac OS X;)"
            )
        }
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

    elif platform == "instagram":
        if INSTAGRAM_COOKIE_FILE:
            opts["cookiefile"] = INSTAGRAM_COOKIE_FILE

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

        # Detect platform early so we can inject the right cookies
        if "tiktok" in url:
            platform_early = "tiktok"
        elif "youtube" in url or "youtu.be" in url:
            platform_early = "youtube"
        else:
            platform_early = "instagram"

        # 2. Extract Metadata
        with yt_dlp.YoutubeDL(get_ydl_opts(platform_early)) as ydl:
            info = ydl.extract_info(url, download=False)

            actual_title = info.get("title", "Untitled_Reel")
            clean_name = sanitize_filename(actual_title)
            filename = f"{clean_name}.mp4"
            local_path = f"/tmp/{filename}"

            uploader = info.get("uploader") or info.get("channel") or "Unknown"
            raw_duration = info.get("duration", 0)
            duration = int(float(raw_duration)) if raw_duration else 0
            description = info.get("description", "") or ""
            upload_date = info.get("upload_date", "")
            audio_track = info.get("track") or info.get("artist") or "Original Audio"
            tags = info.get("tags", [])
            tags_str = ", ".join(tags) if tags else ""

            # New metadata fields
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
        # Format chain: native mp4 first, then any video+audio merge, then absolute best
        # TikTok and YouTube Shorts often don't have separate streams, so "best" is the safe fallback
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

        # 4. Upload video to R2
        public_url = upload_to_r2(local_path, filename)

        # 5. Download & upload thumbnail to R2
        thumbnail_url = ""
        if thumbnail:
            try:
                thumb_resp = requests.get(thumbnail, timeout=10)
                if thumb_resp.status_code == 200:
                    thumb_path = os.path.join(tempfile.gettempdir(), f"{clean_name}_thumb.jpg")
                    with open(thumb_path, "wb") as f:
                        f.write(thumb_resp.content)
                    thumbnail_url = upload_to_r2(thumb_path, f"thumbs/{clean_name}.jpg", content_type="image/jpeg")
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
        return jsonify({"status": "success", "file": filename})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/delete_reel", methods=["POST"])
def delete_reel():
    reel_id = request.json.get("id")
    try:
        record = d1_query("SELECT video_url FROM reels WHERE id = ?", [reel_id])
        if record.get("results"):
            video_url = record["results"][0]["video_url"]
            filename = video_url.split("/")[-1]
            delete_from_r2(filename)

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

        sql = f"SELECT * FROM reels {where} ORDER BY {order} LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        result = d1_query(sql, params)
        return jsonify(result.get("results", []))
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
