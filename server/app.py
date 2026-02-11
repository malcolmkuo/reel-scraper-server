import os
import re
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

TEAM_PASSWORD = os.environ.get("TEAM_PASSWORD", "airlab_secret_2026")

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


def upload_to_r2(local_path, key):
    """Upload a file to R2 and return its public URL."""
    s3.upload_file(local_path, R2_BUCKET_NAME, key, ExtraArgs={"ContentType": "video/mp4"})
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

        # 2. Extract Metadata
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
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

        # 3. Download
        ydl_opts = {"quiet": True, "outtmpl": local_path, "format": "best[ext=mp4]"}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # 4. Upload to R2
        public_url = upload_to_r2(local_path, filename)

        # 5. Insert into D1
        d1_query(
            """INSERT INTO reels (url, video_url, title, added_by, language, description, tags, duration, uploader, upload_date, audio, likes, views, comments, shares)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        sort = request.args.get("sort", "newest").strip()

        clauses = []
        params = []

        if search:
            clauses.append("(title LIKE ? OR uploader LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if language:
            clauses.append("language = ?")
            params.append(language)

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        sort_map = {
            "newest": "id DESC",
            "oldest": "id ASC",
            "most_liked": "likes DESC",
            "most_viewed": "views DESC",
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

        return jsonify({
            "total": total_count,
            "languages": languages,
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
