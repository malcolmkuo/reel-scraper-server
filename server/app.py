import os
import psycopg2
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
# 1. Get Database URL from Cloud (or use local fallback)
DB_URL = os.environ.get("DATABASE_URL", "postgres://user:pass@localhost/dbname")
# 2. Set the Shared Team Password
TEAM_PASSWORD = os.environ.get("TEAM_PASSWORD", "airlab_secret_2026")

# --- DATABASE CONNECTION ---
def get_db_connection():
    try:
        conn = psycopg2.connect(DB_URL)
        return conn
    except Exception as e:
        print(f"❌ DB Connection Error: {e}")
        return None

# --- INITIALIZE TABLES ---
def init_db():
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reels (
                id SERIAL PRIMARY KEY,
                title TEXT,
                url TEXT UNIQUE,
                author TEXT,
                thumbnail TEXT,
                saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Database initialized")

# Run DB setup immediately
init_db()

# --- ROUTES ---

@app.route('/')
def home():
    return "Reel Vault Server is Running! 🚀"

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    if data.get('password') == TEAM_PASSWORD:
        return jsonify({"status": "success", "token": "valid_session"}), 200
    return jsonify({"error": "Wrong Password"}), 401

@app.route('/add_reel', methods=['POST'])
def add_reel():
    data = request.json
    url = data.get('url')

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    print(f"📥 Processing: {url}")

    try:
        # 1. Extract Metadata using yt-dlp (Fast, no download needed yet)
        ydl_opts = {'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Untitled')
            author = info.get('uploader', 'Unknown')
            thumbnail = info.get('thumbnail', '')

        # 2. Save to Cloud Database
        conn = get_db_connection()
        cur = conn.cursor()
        # Check for duplicates
        cur.execute("SELECT id FROM reels WHERE url = %s", (url,))
        if cur.fetchone():
            return jsonify({"status": "duplicate", "message": "Reel already in Vault!"})

        cur.execute(
            "INSERT INTO reels (title, url, author, thumbnail) VALUES (%s, %s, %s, %s)",
            (title, url, author, thumbnail)
        )
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({
            "status": "success", 
            "title": title,
            "message": "Saved to Team Vault"
        })

    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/library', methods=['GET'])
def get_library():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT title, url, author, thumbnail FROM reels ORDER BY saved_at DESC")
        rows = cur.fetchall()
        
        library = []
        for row in rows:
            library.append({
                "title": row[0],
                "url": row[1],
                "author": row[2],
                "thumbnail": row[3]
            })
        
        cur.close()
        conn.close()
        return jsonify(library)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)