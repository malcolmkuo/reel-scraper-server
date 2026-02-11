import os
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
from supabase import create_client, Client

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
# Get these from Render Environment Variables
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BUCKET_NAME = "videos"
TEAM_PASSWORD = os.environ.get("TEAM_PASSWORD", "malithegoat123")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def sanitize_filename(name):
    """Removes illegal characters and spaces for clean cloud storage."""
    # Replace invalid chars with empty string, replace spaces with underscores
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    return clean.replace(" ", "_")

@app.route('/')
def home():
    return "Reel Vault Engine Online 🟢"

# --- AUTHENTICATION ---
@app.route('/login', methods=['POST'])
def login():
    if request.json.get('password') == TEAM_PASSWORD:
        return jsonify({"status": "success"}), 200
    return jsonify({"error": "Unauthorized"}), 401

# --- CORE LOGIC ---
@app.route('/add_reel', methods=['POST'])
def add_reel():
    url = request.json.get('url')
    if not url: return jsonify({"error": "No URL provided"}), 400

    try:
        # 1. DUPLICATE CHECK
        # Don't download if we already have it
        existing = supabase.table("reels").select("id").eq("url", url).execute()
        if existing.data:
            return jsonify({"status": "exists", "message": "Reel already in vault"}), 200

        # 2. FETCH METADATA (To get the Title)
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            actual_title = info.get('title', 'Untitled_Reel')
            
            # Create a clean filename like "Funny_Cat_Video.mp4"
            clean_name = sanitize_filename(actual_title)
            filename = f"{clean_name}.mp4"
            local_path = f"/tmp/{filename}"

        # 3. DOWNLOAD VIDEO
        ydl_opts = {
            'quiet': True,
            'outtmpl': local_path,
            'format': 'best[ext=mp4]', # Force MP4 for mobile compatibility
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # 4. UPLOAD TO STORAGE
        with open(local_path, 'rb') as f:
            supabase.storage.from_(BUCKET_NAME).upload(
                path=filename,
                file=f,
                file_options={"content-type": "video/mp4"}
            )
        
        # 5. SAVE DATA TO DB
        public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(filename)
        data = {
            "url": url,
            "video_url": public_url,
            "title": actual_title,
            "likes": info.get('like_count', 0),
            "views": info.get('view_count', 0),
            "comments": info.get('comment_count', 0),
            "shares": info.get('repost_count', 0)
        }
        supabase.table("reels").insert(data).execute()

        # Cleanup
        if os.path.exists(local_path): os.remove(local_path)

        return jsonify({"status": "success", "file": filename})

    except Exception as e:
        print(f"Server Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- LIBRARY FETCH ---
@app.route('/library', methods=['GET'])
def get_library():
    try:
        # Get last 20 saved items
        res = supabase.table("reels").select("*").order("id", desc=True).limit(20).execute()
        return jsonify(res.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)