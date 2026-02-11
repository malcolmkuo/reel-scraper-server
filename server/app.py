import os
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
from supabase import create_client, Client

app = Flask(__name__)
CORS(app)

# CONFIGURATION
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BUCKET_NAME = "videos"
TEAM_PASSWORD = os.environ.get("TEAM_PASSWORD", "airlab_secret_2026")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def sanitize_filename(name):
    """Removes invalid characters for cloud storage."""
    return re.sub(r'[\\/*?:"<>|]', "", name)

@app.route('/')
def home():
    return "Reel Scraper Cloud Engine Online"

@app.route('/login', methods=['POST'])
def login():
    if request.json.get('password') == TEAM_PASSWORD:
        return jsonify({"status": "success"}), 200
    return jsonify({"error": "Unauthorized"}), 401

@app.route('/add_reel', methods=['POST'])
def add_reel():
    url = request.json.get('url')
    if not url: return jsonify({"error": "No URL"}), 400

    try:
        # 1. FETCH METADATA FIRST
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            raw_title = info.get('title', 'Untitled')
            clean_title = sanitize_filename(raw_title)
            filename = f"{clean_title}.mp4"
            local_path = f"/tmp/{filename}"

        # 2. DOWNLOAD VIDEO
        ydl_opts = {
            'quiet': True,
            'outtmpl': local_path,
            'format': 'best[ext=mp4]',
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # 3. UPLOAD TO SUPABASE
        with open(local_path, 'rb') as f:
            supabase.storage.from_(BUCKET_NAME).upload(
                path=filename,
                file=f,
                file_options={"content-type": "video/mp4"}
            )
        
        # 4. SAVE DATA TO DB
        public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(filename)
        data = {
            "url": url,
            "video_url": public_url,
            "title": raw_title,
            "likes": info.get('like_count', 0),
            "views": info.get('view_count', 0),
            "comments": info.get('comment_count', 0),
            "shares": info.get('repost_count', 0)
        }
        supabase.table("reels").insert(data).execute()

        if os.path.exists(local_path): os.remove(local_path)
        return jsonify({"status": "success", "video": filename})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/library', methods=['GET'])
def get_library():
    try:
        res = supabase.table("reels").select("*").order("id", desc=True).limit(20).execute()
        return jsonify(res.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)