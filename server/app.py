import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
from supabase import create_client, Client

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BUCKET_NAME = "videos" # Matches your screenshot
TEAM_PASSWORD = os.environ.get("TEAM_PASSWORD", "malithegoat123")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.route('/')
def home():
    return "Reel Scraper (Matches Screenshots) Running"

# --- LOGIN ROUTE ---
@app.route('/login', methods=['POST'])
def login():
    if request.json.get('password') == TEAM_PASSWORD:
        return jsonify({"status": "success"}), 200
    return jsonify({"error": "Wrong Password"}), 401

# --- SAVE REEL ROUTE ---
@app.route('/add_reel', methods=['POST'])
def add_reel():
    url = request.json.get('url')
    if not url: return jsonify({"error": "No URL provided"}), 400

    try:
        # 1. DOWNLOAD VIDEO & EXTRACT DATA
        ydl_opts = {
            'quiet': True,
            'outtmpl': '/tmp/%(id)s.%(ext)s',
            'format': 'best[ext=mp4]',
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            video_id = info.get('id')
            filename = f"{video_id}.mp4"
            local_path = f"/tmp/{filename}"
            
            # Prepare Data Packet (Matches your Table Columns)
            data = {
                "url": url,
                "title": info.get('title', 'Untitled'),
                "likes": info.get('like_count', 0),
                "views": info.get('view_count', 0),
                "comments": info.get('comment_count', 0),
                "shares": info.get('repost_count', 0)
            }

        # 2. UPLOAD TO 'videos' BUCKET
        with open(local_path, 'rb') as f:
            supabase.storage.from_(BUCKET_NAME).upload(
                path=filename,
                file=f,
                file_options={"content-type": "video/mp4"}
            )
        
        # 3. GET STREAMING LINK
        public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(filename)
        data['video_url'] = public_url 

        # 4. SAVE TO DATABASE
        supabase.table("reels").insert(data).execute()

        # Clean up
        if os.path.exists(local_path): os.remove(local_path)

        return jsonify({"status": "success", "data": data})

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

# --- LIBRARY ROUTE ---
@app.route('/library', methods=['GET'])
def get_library():
    try:
        response = supabase.table("reels").select("*").order("id", desc=True).limit(50).execute()
        return jsonify(response.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)