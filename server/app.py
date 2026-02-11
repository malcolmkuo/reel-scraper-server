import os
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
from supabase import create_client, Client

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BUCKET_NAME = "videos"
TEAM_PASSWORD = os.environ.get("TEAM_PASSWORD", "airlab_secret_2026")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def sanitize_filename(name):
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    return clean.replace(" ", "_")

@app.route('/')
def home():
    return "Reel Vault Engine Online"

@app.route('/login', methods=['POST'])
def login():
    if request.json.get('password') == TEAM_PASSWORD:
        return jsonify({"status": "success"}), 200
    return jsonify({"error": "Unauthorized"}), 401

@app.route('/add_reel', methods=['POST'])
def add_reel():
    data = request.json
    url = data.get('url')
    username = data.get('username', 'Anonymous')
    language = data.get('language', 'English')

    if not url: return jsonify({"error": "No URL provided"}), 400

    try:
        # 1. Check Duplicates
        existing = supabase.table("reels").select("id").eq("url", url).execute()
        if existing.data:
            return jsonify({"status": "exists", "message": "Reel already in vault"}), 200

        # 2. Extract Metadata (No Thumbnail)
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            
            actual_title = info.get('title', 'Untitled_Reel')
            clean_name = sanitize_filename(actual_title)
            filename = f"{clean_name}.mp4"
            local_path = f"/tmp/{filename}"

            # Prediction Features
            uploader = info.get('uploader') or info.get('channel') or "Unknown"
            duration = info.get('duration', 0)
            description = info.get('description', '') or ''
            upload_date = info.get('upload_date', '') 
            audio_track = info.get('track') or info.get('artist') or "Original Audio"
            
            tags = info.get('tags', []) 
            tags_str = ", ".join(tags) if tags else ""

        # 3. Download
        ydl_opts = {'quiet': True, 'outtmpl': local_path, 'format': 'best[ext=mp4]'}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # 4. Upload
        with open(local_path, 'rb') as f:
            supabase.storage.from_(BUCKET_NAME).upload(
                path=filename,
                file=f,
                file_options={"content-type": "video/mp4"}
            )
        
        public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(filename)
        
        # 5. Insert into Database
        db_data = {
            "url": url,
            "video_url": public_url,
            "title": actual_title,
            "added_by": username,
            "language": language,
            
            # RecSys Features (Minus Thumbnail)
            "description": description,
            "tags": tags_str,
            "duration": duration,
            "uploader": uploader,
            "upload_date": upload_date,
            "audio": audio_track,

            # Metrics
            "likes": info.get('like_count', 0),
            "views": info.get('view_count', 0),
            "comments": info.get('comment_count', 0),
            "shares": info.get('repost_count', 0)
        }
        supabase.table("reels").insert(db_data).execute()

        if os.path.exists(local_path): os.remove(local_path)
        return jsonify({"status": "success", "file": filename})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/delete_reel', methods=['POST'])
def delete_reel():
    reel_id = request.json.get('id')
    try:
        record = supabase.table("reels").select("video_url").eq("id", reel_id).execute()
        if record.data:
            filename = record.data[0]['video_url'].split('/')[-1]
            supabase.storage.from_(BUCKET_NAME).remove([filename])
        
        supabase.table("reels").delete().eq("id", reel_id).execute()
        return jsonify({"status": "deleted"}), 200
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