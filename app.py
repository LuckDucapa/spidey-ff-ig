from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from itertools import islice
import instaloader
import re
import time
import datetime

app = Flask(__name__)
CORS(app)

# Keep JSON order preserved
app.json.sort_keys = False 

# ==========================================================
#                   HELPER FUNCTIONS
# ==========================================================

def get_shortcode(url):
    if not "instagram.com" in url and not "http" in url:
        return url.strip()
    match = re.search(r'(?:reel|reels|p|tv)/([^/?#&]+)', url)
    return match.group(1) if match else url.strip('/')

def safe_int(value):
    if value is None: return 0
    try: return int(value)
    except: return 0

def get_iso_date(date_obj):
    if not date_obj: return None
    return date_obj.strftime('%Y-%m-%d %H:%M:%S')

def extract_music(node):
    try:
        # Try finding music info in the node
        if 'clips_music_attribution_info' in node:
            m = node['clips_music_attribution_info']
            if m:
                return {
                    "artist_name": m.get('artist_name', 'Unknown'),
                    "song_name": m.get('song_name', 'Unknown'),
                    "uses_original_audio": m.get('uses_original_audio', False),
                    "audio_id": m.get('audio_id')
                }
    except: pass
    return None

def extract_tagged(post):
    tagged = []
    try:
        edges = post._node.get('edge_media_to_tagged_user', {}).get('edges', [])
        for edge in edges:
            u = edge.get('node', {}).get('user', {})
            tagged.append({
                "username": u.get('username'),
                "name": u.get('full_name'),
                "is_verified": u.get('is_verified', False)
            })
    except: pass
    
    # Fallback to Instaloader's property if manual extraction fails
    if not tagged and post.tagged_users:
        for u in post.tagged_users:
            if not isinstance(u, str): # Verify it's an object
                tagged.append({
                    "username": u.username,
                    "name": u.full_name,
                    "is_verified": u.is_verified
                })
    return tagged

def extract_carousel(post):
    items = []
    if post.typename == 'GraphSidecar':
        try:
            for i, node in enumerate(post.get_sidecar_nodes(), 1):
                is_video = node.is_video
                media_type = "video" if is_video else "image"
                url = node.video_url if is_video else node.display_url
                items.append({
                    "position": i,
                    "id": getattr(node, 'shortcode', f"{post.shortcode}_{i}"),
                    "type": media_type,
                    "link": url,
                    "width": 1080, # Placeholder as node dims are tricky
                    "height": 1350
                })
        except: pass
    return items

def format_post_object(post, position=None):
    # Views logic
    views = post.video_view_count
    if views is None: views = post._node.get('video_view_count')
    if views is None: views = post._node.get('play_count')

    # Dimensions
    width = getattr(post, 'width', None)
    height = getattr(post, 'height', None)
    if not width:
        d = post._node.get('dimensions', {})
        width = d.get('width', 0)
        height = d.get('height', 0)

    p_type = "image"
    if post.typename == 'GraphVideo': p_type = "reel"
    if post.typename == 'GraphSidecar': p_type = "carousel"

    obj = {
        "position": position if position else 1,
        "id": post.shortcode,
        "permalink": f"https://www.instagram.com/p/{post.shortcode}/",
        "type": p_type,
        "link": post.video_url if post.is_video else post.url,
        "width": width,
        "height": height,
        "views": views if views else 0,
        "caption": post.caption if post.caption else "",
        "likes": post.likes,
        "comments": post.comments,
        "iso_date": get_iso_date(post.date_local),
        "thumbnail": post.url
    }
    
    # Optional fields to keep JSON clean
    if not position: del obj["position"]
    if not views: del obj["views"]

    music = extract_music(post._node)
    if music:
        obj["music"] = music
        obj["has_audio"] = True
    
    tags = extract_tagged(post)
    if tags: obj["tagged_users"] = tags

    if p_type == "carousel":
        c_items = extract_carousel(post)
        if c_items: obj["carousel_items"] = c_items

    return obj

# ==========================================================
#                       MAIN ROUTE
# ==========================================================

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/ig', methods=['GET'])
def instagram_api():
    start_time = time.time()
    
    url = request.args.get('url')
    username = request.args.get('username')

    if not url and not username:
        return jsonify({"status": "Error", "message": "Missing parameters"}), 400

    L = instaloader.Instaloader()
    # Strong User-Agent from your Termux script
    L.context._session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    })

    try:
        # ==========================
        # MODE A: PROFILE SEARCH
        # ==========================
        if username:
            clean_username = username.replace('@', '').strip()
            try:
                profile = instaloader.Profile.from_username(L.context, clean_username)
            except Exception as e:
                return jsonify({"status": "Error", "message": f"Profile not found: {str(e)}"}), 404

            # Extract Bio Links (Rich Data)
            bio_links = []
            if hasattr(profile, '_node'):
                bio_links = profile._node.get('bio_links', [])
            if not bio_links and profile.external_url:
                bio_links = [{'title': 'External Link', 'url': profile.external_url}]

            # Fetch Posts
            posts_list = []
            try:
                for i, post in enumerate(islice(profile.get_posts(), 6), 1):
                    posts_list.append(format_post_object(post, position=i))
            except: pass

            response = {
                "type": "profile",
                "profile": {
                    "username": profile.username,
                    "name": profile.full_name,
                    "avatar": profile.profile_pic_url,
                    "avatar_hd": profile.profile_pic_url, # Usually same unless logged in
                    "is_verified": profile.is_verified,
                    "is_business": profile.is_business_account,
                    "posts_count": profile.mediacount,
                    "followers": profile.followers,
                    "following": profile.followees,
                    "bio": profile.biography,
                    "bio_links": bio_links
                },
                "latest_posts": posts_list
            }
            return jsonify(response), 200

        # ==========================
        # MODE B: MEDIA SEARCH
        # ==========================
        elif url:
            shortcode = get_shortcode(url)
            
            try:
                post = instaloader.Post.from_shortcode(L.context, shortcode)
            except Exception as e:
                return jsonify({"status": "Error", "message": f"Post not found: {str(e)}"}), 404

            # Force Metadata Load for Author
            author = {}
            bio_links = []
            try:
                p = post.owner_profile
                _ = p.biography # Trigger load
                
                # Get Bio Links from Author
                if hasattr(p, '_node'): bio_links = p._node.get('bio_links', [])
                if not bio_links and p.external_url: bio_links = [{'title': 'External', 'url': p.external_url}]
                
                author = {
                    "username": p.username,
                    "name": p.full_name,
                    "id": p.userid,
                    "verified": p.is_verified,
                    "business": p.is_business_account,
                    "followers": p.followers,
                    "following": p.followees,
                    "posts": p.mediacount,
                    "bio": p.biography,
                    "avatar": p.profile_pic_url
                }
            except:
                # Fallback if profile load fails (Login required scenarios)
                author = {"username": post.owner_username, "id": post.owner_id, "note": "Detailed info hidden"}

            # Get Detailed Specs
            specs = format_post_object(post)
            
            # Construct Final Rich JSON
            response = {
                "type": "media",
                "author_details": {
                    "Username": f"@{author.get('username')}",
                    "Full Name": author.get('name', ''),
                    "User ID": author.get('id'),
                    "Verified": str(author.get('verified', False)),
                    "Business": str(author.get('business', False)),
                    "Followers": f"{safe_int(author.get('followers')):,}",
                    "Following": f"{safe_int(author.get('following')):,}",
                    "Total Posts": f"{safe_int(author.get('posts')):,}",
                    "Bio": author.get('bio', 'Empty'),
                    "HD Avatar": author.get('avatar')
                },
                "bio_links": bio_links,
                "audio": specs.get('music', "No music metadata found (or Image Post)."),
                "reel_specs": {
                    "Type": specs['type'],
                    "Dimensions": f"{specs['width']} x {specs['height']}",
                    "Duration": f"{post.video_duration} sec" if post.video_duration else "N/A",
                    "Upload Date": str(post.date_local),
                    "Shortcode": post.shortcode
                },
                "engagement": {
                    "Views": f"{safe_int(specs.get('views')):,}" if specs.get('views') else "N/A",
                    "Likes": f"{safe_int(post.likes):,}",
                    "Comments": f"{safe_int(post.comments):,}"
                },
                "tagged_users": specs.get('tagged_users', []),
                "caption": post.caption if post.caption else "",
                "downloads": {
                    "Thumbnail": post.url,
                    "Download URL": post.video_url if post.is_video else post.url
                }
            }
            
            # Handling Carousel
            if 'carousel_items' in specs:
                response['carousel_items'] = specs['carousel_items']

            return jsonify(response), 200

    except Exception as e:
        return jsonify({"status": "Error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
