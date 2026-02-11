import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
from supabase import create_client, Client

app = Flask(__name__)
CORS(app)

# CONFIGURATION
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
BUCKET_NAME = "reels_videos"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.route('/')
def home():
    return "Reel Scraper (Full Data) Running"

@app.route('/add_reel', methods=['POST'])
def add_reel():
    url = request.json.get('url')
    if not url: return jsonify({"error": "No URL provided"}), 400

    try:
        # 1. DOWNLOAD VIDEO & EXTRACT DATA
        # We need the file for the scrolling feature, and metadata for your DB
        ydl_opts = {
            'quiet': True,
            'outtmpl': '/tmp/%(id)s.%(ext)s',
            'format': 'best[ext=mp4]',
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            # File info
            video_id = info.get('id')
            filename = f"{video_id}.mp4"
            local_path = f"/tmp/{filename}"
            
            # Extract the 6 specific data points you requested
            # Note: Instagram often hides 'shares' (repost_count) from scrapers
            data = {
                "url": url,
                "title": info.get('title', 'Untitled'),
                "likes": info.get('like_count', 0),
                "views": info.get('view_count', 0),
                "comments": info.get('comment_count', 0),
                "shares": info.get('repost_count', 0)
            }

        # 2. UPLOAD TO STORAGE (For Fast Scrolling)
        with open(local_path, 'rb') as f:
            supabase.storage.from_(BUCKET_NAME).upload(
                path=filename,
                file=f,
                file_options={"content-type": "video/mp4"}
            )
        
        # Get the Fast CDN Link
        public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(filename)
        
        # Add the CDN link to the data packet
        data['video_url'] = public_url

        # 3. SAVE TO DATABASE
        supabase.table("reels").insert(data).execute()

        # Clean up local file
        if os.path.exists(local_path): os.remove(local_path)

        return jsonify({
            "status": "success", 
            "data": data
        })

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)