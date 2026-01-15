from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from itertools import islice
import instaloader
import re
import time
import datetime
import os

app = Flask(__name__)
CORS(app) # <--- FIXES CORS ERROR

# Disable alphabetical sorting to keep JSON order
app.json.sort_keys = False 

# ==========================================================
#                   HELPER FUNCTIONS
# ==========================================================

def get_shortcode(url):
    """Robust extraction of shortcode/ID."""
    if not "instagram.com" in url:
        return url.strip()
    match = re.search(r'(?:reel|reels|p|tv)/([^/?#&]+)', url)
    return match.group(1) if match else url.strip('/')

def safe_int(value):
    try: return int(value)
    except: return 0

def get_iso_date(date_obj):
    return date_obj.strftime('%Y-%m-%dT%H:%M:%SZ') if date_obj else None

def extract_music(node):
    try:
        m = node.get('clips_music_attribution_info')
        if m:
            return {
                "artist_name": m.get('artist_name', 'Unknown'),
                "song_name": m.get('song_name', 'Unknown'),
                "audio_id": m.get('audio_id')
            }
    except: pass
    return "No Music / Original Audio"

def format_post_object(post, position=None):
    width = getattr(post, 'width', 0)
    height = getattr(post, 'height', 0)
    
    p_type = "image"
    if post.typename == 'GraphVideo': p_type = "reel"
    if post.typename == 'GraphSidecar': p_type = "carousel"

    obj = {
        "position": position if position else 1,
        "id": post.shortcode,
        "permalink": f"https://www.instagram.com/p/{post.shortcode}/",
        "type": p_type,
        "link": post.video_url if post.is_video else post.url,
        "views": post.video_view_count if post.video_view_count else 0,
        "likes": post.likes,
        "comments": post.comments,
        "date": get_iso_date(post.date_local),
        "thumbnail": post.url
    }
    if not position: del obj["position"]
    return obj

# ==========================================================
#                       ROUTES
# ==========================================================

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/ig', methods=['GET'])
def instagram_api():
    start_time = time.time()
    
    # Get parameters
    url_input = request.args.get('url')
    username_input = request.args.get('username')

    # Initialize Instaloader with User Agent override
    L = instaloader.Instaloader()
    # iPhone User Agent to try and bypass login blocks
    L.context._session.headers.update({
        'User-Agent': 'Instagram 219.0.0.12.117 Android (31/12; 480dpi; 1080x2052; Samsung; SM-G996B; p3s; exynos2100; en_DE; 344849646)'
    })

    try:
        # --- SCENARIO 1: USERNAME SEARCH ---
        if username_input:
            clean_username = username_input.replace('@', '').strip()
            
            try:
                profile = instaloader.Profile.from_username(L.context, clean_username)
            except Exception as e:
                return jsonify({"status": "Error", "message": "Profile not found (or IP Blocked)"}), 404

            posts_list = []
            try:
                # Limit to 6 posts for speed
                for i, post in enumerate(islice(profile.get_posts(), 6), 1):
                    posts_list.append(format_post_object(post, position=i))
            except: pass

            response = {
                "type": "profile",
                "profile": {
                    "username": profile.username,
                    "name": profile.full_name,
                    "avatar": profile.profile_pic_url,
                    "followers": profile.followers,
                    "following": profile.followees,
                    "posts_count": profile.mediacount,
                    "bio": profile.biography,
                    "external_link": profile.external_url
                },
                "latest_posts": posts_list
            }
            return jsonify(response), 200

        # --- SCENARIO 2: URL/REEL SEARCH ---
        elif url_input:
            shortcode = get_shortcode(url_input)
            
            try:
                post = instaloader.Post.from_shortcode(L.context, shortcode)
            except Exception as e:
                return jsonify({"status": "Error", "message": "Post not found or Private"}), 404

            # Extract Author
            author = {"username": post.owner_username, "id": post.owner_id}
            try:
                p = post.owner_profile
                author['name'] = p.full_name
                author['followers'] = p.followers
                author['avatar'] = p.profile_pic_url
            except: pass

            specs = format_post_object(post)
            music = extract_music(post._node)

            response = {
                "type": "media",
                "author_details": {
                    "Username": f"@{author.get('username')}",
                    "Full Name": author.get('name', 'N/A'),
                    "Followers": f"{safe_int(author.get('followers')):,}",
                    "Avatar": author.get('avatar', '')
                },
                "media_details": {
                    "Type": specs['type'],
                    "Shortcode": post.shortcode,
                    "Duration": f"{post.video_duration}s" if post.video_duration else "Image",
                    "Date": str(post.date_local),
                    "Views": f"{safe_int(specs.get('views')):,}",
                    "Likes": f"{safe_int(post.likes):,}",
                    "Comments": f"{safe_int(post.comments):,}"
                },
                "audio": music,
                "caption": post.caption if post.caption else "",
                "downloads": {
                    "Thumbnail": post.url,
                    "Download URL": post.video_url if post.is_video else post.url
                }
            }
            return jsonify(response), 200
        
        else:
            return jsonify({"status": "Error", "message": "Please provide 'url' or 'username' parameter"}), 400

    except Exception as e:
        return jsonify({"status": "Error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
