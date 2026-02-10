import os
import sqlite3
import psycopg2
from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

# CONFIGURATION
# Uses Cloud Postgres if available, otherwise local SQLite
DB_URL = os.environ.get("DATABASE_URL")
TEAM_PASSWORD = os.environ.get("TEAM_PASSWORD", "airlab_secret_2026")

def get_db_connection():
    if DB_URL:
        return psycopg2.connect(DB_URL)
    # Local fallback to SQLite
    conn = sqlite3.connect('local_reels.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    if DB_URL:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reels (
                id SERIAL PRIMARY KEY, title TEXT, url TEXT UNIQUE, 
                author TEXT, thumbnail TEXT, saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reels (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, url TEXT UNIQUE, 
                author TEXT, thumbnail TEXT, saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
    conn.commit()
    conn.close()

init_db()

# ROUTES

@app.route('/login', methods=['POST'])
def login():
    if request.json.get('password') == TEAM_PASSWORD:
        return jsonify({"status": "success"}), 200
    return jsonify({"error": "Wrong Password"}), 401

@app.route('/add_reel', methods=['POST'])
def add_reel():
    url = request.json.get('url')
    if not url: return jsonify({"error": "No URL"}), 400

    try:
        ydl_opts = {'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'Untitled')
            author = info.get('uploader', 'Unknown')
            thumbnail = info.get('thumbnail', '')

        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check duplicate
        q_check = "SELECT id FROM reels WHERE url = %s" if DB_URL else "SELECT id FROM reels WHERE url = ?"
        cur.execute(q_check, (url,))
        if cur.fetchone():
            return jsonify({"status": "duplicate"})

        # Insert
        q_ins = "INSERT INTO reels (title, url, author, thumbnail) VALUES (%s, %s, %s, %s)" if DB_URL else "INSERT INTO reels (title, url, author, thumbnail) VALUES (?, ?, ?, ?)"
        cur.execute(q_ins, (title, url, author, thumbnail))
        
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "title": title})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/library', methods=['GET'])
def get_library():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT title, url, author, thumbnail FROM reels ORDER BY saved_at DESC LIMIT 10")
        rows = cur.fetchall()
        library = [{"title": r[0], "url": r[1], "author": r[2], "thumbnail": r[3]} for r in rows]
        conn.close()
        return jsonify(library)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Use 5001 to avoid macOS AirPlay conflict on 5000
    port = int(os.environ.get("PORT", 5001))
    app.run(host='0.0.0.0', port=port)