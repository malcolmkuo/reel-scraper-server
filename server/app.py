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

# Initialize Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.route('/')
def home():
    return "Supabase Scraper Running"

@app.route('/add_reel', methods=['POST'])
def add_reel():
    # 1. Get the URL from the request
    url = request.json.get('url')
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        # 2. Extract Metadata using yt-dlp
        # We use 'extract_flat' for speed, or normal extraction for details
        ydl_opts = {
            'quiet': True,
            'skip_download': True,
            'no_warnings': True,
        }
        
        data_to_save = {}

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Map yt-dlp data to your Supabase columns
            data_to_save = {
                "url": url,
                "title": info.get('title', 'Untitled'),
                "likes": info.get('like_count', 0),
                "views": info.get('view_count', 0),
                "shares": info.get('repost_count', 0) # Note: IG often hides this field
            }

        # 3. Insert into Supabase 'reels' table
        response = supabase.table("reels").insert(data_to_save).execute()

        return jsonify({
            "status": "success", 
            "data": data_to_save
        })

    except Exception as e:
        print(f"Error processing reel: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)