from flask import Flask, request, jsonify
from itertools import islice
import instaloader
import re
import time
import datetime

app = Flask(__name__)

# ==========================================================
#                   HELPER FUNCTIONS
# ==========================================================

def get_shortcode(url):
    match = re.search(r'instagram\.com/(?:reel|p)/([^/?#&]+)', url)
    if match: return match.group(1)
    return url.strip('/')

def safe_int(value):
    if value is None: return 0
    try: return int(value)
    except: return 0

def get_iso_date(date_obj):
    if not date_obj: return None
    # Add UTC 'Z' or timezone offset if possible, here we keep it simple ISO
    return date_obj.strftime('%Y-%m-%dT%H:%M:%SZ')

def extract_music(node):
    """Extracts music info from raw node."""
    try:
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
    """Deep extracts tagged users."""
    tagged = []
    try:
        # Check raw edges first for more data
        edges = post._node.get('edge_media_to_tagged_user', {}).get('edges', [])
        for edge in edges:
            u = edge.get('node', {}).get('user', {})
            tagged.append({
                "username": u.get('username'),
                "name": u.get('full_name'),
                "is_verified": u.get('is_verified', False)
            })
    except: pass
    
    # Fallback to standard if raw failed
    if not tagged and post.tagged_users:
        for u in post.tagged_users:
            # Handle string vs object
            if not isinstance(u, str):
                tagged.append({
                    "username": u.username,
                    "name": u.full_name,
                    "is_verified": u.is_verified
                })
    return tagged

def extract_carousel(post):
    """Extracts sidecar items."""
    items = []
    if post.typename == 'GraphSidecar':
        try:
            for i, node in enumerate(post.get_sidecar_nodes(), 1):
                # Determine type
                is_video = node.is_video
                media_type = "video" if is_video else "image"
                url = node.video_url if is_video else node.display_url
                
                # Try to get Dimensions
                # Instaloader SidecarNode structure varies, try safe access
                n_width = 1080
                n_height = 1350
                # Dimensions aren't always exposed in SidecarNode iterator easily without accessing private dict
                
                items.append({
                    "position": i,
                    "id": node.shortcode if hasattr(node, 'shortcode') else f"{post.shortcode}_{i}",
                    "type": media_type,
                    "link": url,
                    "width": n_width, # Placeholder if not available
                    "height": n_height
                })
        except: pass
    return items

def format_post_object(post, position=None):
    """Formats a post exactly as requested in the JSON example."""
    
    # Views/Plays logic
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

    # Type
    p_type = "image"
    if post.typename == 'GraphVideo': p_type = "reel"
    if post.typename == 'GraphSidecar': p_type = "carousel"

    # Base Object
    obj = {
        "id": post.shortcode,
        "permalink": f"https://www.instagram.com/p/{post.shortcode}/",
        "type": p_type,
        "link": post.video_url if post.is_video else post.url,
        "width": width,
        "height": height,
        "caption": post.caption,
        "likes": post.likes,
        "comments": post.comments,
        "iso_date": get_iso_date(post.date_local),
        "thumbnail": post.url
    }

    if position:
        obj["position"] = position

    if views:
        obj["views"] = views

    # Audio/Music
    music = extract_music(post._node)
    if music:
        obj["music"] = music
        obj["has_audio"] = True
    elif post.is_video:
        obj["has_audio"] = False # Default if no music node found

    # Tagged Users
    tags = extract_tagged(post)
    if tags:
        obj["tagged_users"] = tags

    # Carousel Items
    if p_type == "carousel":
        c_items = extract_carousel(post)
        if c_items:
            obj["carousel_items"] = c_items

    return obj

# ==========================================================
#                       MAIN ROUTE
# ==========================================================

@app.route('/ig', methods=['GET'])
def instagram_api():
    start_time = time.time()
    
    # Arguments
    url = request.args.get('url')
    pid = request.args.get('id')
    username = request.args.get('username')
    userid = request.args.get('userid')

    # Initialize Instaloader
    L = instaloader.Instaloader()
    L.context._session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    })

    try:
        # ====================================================
        #            MODE A: PROFILE SEARCH
        # ====================================================
        if username or userid:
            target = username if username else userid
            
            # Fetch Profile
            profile = None
            try:
                if username:
                    profile = instaloader.Profile.from_username(L.context, username)
                else:
                    profile = instaloader.Profile.from_id(L.context, int(userid))
            except Exception as e:
                return jsonify({"status": "Error", "message": "Profile not found or Login Required"}), 404

            # Extract Bio Links
            bio_links = []
            if hasattr(profile, '_node'):
                bio_links = profile._node.get('bio_links', [])
            if not bio_links and profile.external_url:
                bio_links = [{'title': 'External Link', 'url': profile.external_url}]

            # Fetch Recent Posts (Limit 12)
            posts_list = []
            try:
                for i, post in enumerate(islice(profile.get_posts(), 12), 1):
                    posts_list.append(format_post_object(post, position=i))
            except: pass

            # Calculate times
            end_time = time.time()
            taken = round(end_time - start_time, 2)

            # Construct Final JSON
            response = {
                "search_metadata": {
                    "id": f"search_{int(start_time)}",
                    "status": "Success",
                    "created_at": datetime.datetime.utcnow().isoformat() + "Z",
                    "request_time_taken": taken,
                    "parsing_time_taken": 0.0, # Placeholder
                    "total_time_taken": taken,
                    "request_url": f"https://www.instagram.com/{profile.username}"
                },
                "search_parameters": {
                    "engine": "instagram_profile",
                    "username": profile.username
                },
                "profile": {
                    "username": profile.username,
                    "name": profile.full_name,
                    "avatar": profile.profile_pic_url,
                    "avatar_hd": profile.profile_pic_url, # HD usually requires login, using standard
                    "is_verified": profile.is_verified,
                    "is_business": profile.is_business_account,
                    "posts": profile.mediacount,
                    "followers": profile.followers,
                    "following": profile.followees,
                    "external_link": profile.external_url,
                    "bio_links": bio_links
                },
                "posts": posts_list
            }
            return jsonify(response), 200

        # ====================================================
        #            MODE B: SINGLE MEDIA SEARCH
        # ====================================================
        elif url or pid:
            target = url if url else pid
            shortcode = get_shortcode(target)
            
            try:
                post = instaloader.Post.from_shortcode(L.context, shortcode)
            except Exception:
                return jsonify({"status": "Error", "message": "Post not found"}), 404

            # Fetch Author Info (Lightweight)
            author = None
            bio_links = []
            try:
                p = post.owner_profile
                _ = p.biography # Force load
                
                # Links
                if hasattr(p, '_node'): bio_links = p._node.get('bio_links', [])
                if not bio_links and p.external_url: bio_links = [{'title': 'External', 'url': p.external_url}]
                
                author = {
                    "username": p.username,
                    "full_name": p.full_name,
                    "user_id": p.userid,
                    "is_verified": p.is_verified,
                    "is_business": p.is_business_account,
                    "followers": p.followers,
                    "following": p.followees,
                    "total_posts": p.mediacount,
                    "biography": p.biography,
                    "hd_avatar": p.profile_pic_url
                }
            except:
                # Fallback if profile blocked
                author = {
                    "username": post.owner_username,
                    "user_id": post.owner_id,
                    "note": "Profile details hidden"
                }

            # Post Details
            media_specs = format_post_object(post) # Reuse logic

            # Convert to "Visual Text-Like" JSON Structure
            # The user asked for JSON, but structured like the text output example
            
            response = {
                "author_details": {
                    "Username": f"@{author.get('username')}",
                    "Full Name": author.get('full_name'),
                    "User ID": author.get('user_id'),
                    "Verified": author.get('is_verified'),
                    "Business": author.get('is_business'),
                    "Followers": f"{safe_int(author.get('followers')):,}",
                    "Following": f"{safe_int(author.get('following')):,}",
                    "Total Posts": f"{safe_int(author.get('total_posts')):,}",
                    "Bio": author.get('biography', 'Empty'),
                    "HD Avatar": author.get('hd_avatar')
                },
                "bio_links": bio_links,
                "audio": media_specs.get('music', "No music metadata found (or Image Post)."),
                "reel_specs": {
                    "Type": media_specs['type'],
                    "Dimensions": f"{media_specs['width']} x {media_specs['height']}",
                    "Duration": "N/A" if media_specs['type'] == 'image' else f"{post.video_duration} sec",
                    "Upload Date": post.date_local.strftime('%Y-%m-%d %H:%M:%S'),
                    "Shortcode": post.shortcode
                },
                "engagement": {
                    "Views": f"{safe_int(media_specs.get('views', 0)):,}" if media_specs.get('views') else "N/A",
                    "Likes": f"{safe_int(post.likes):,}",
                    "Comments": f"{safe_int(post.comments):,}"
                },
                "tagged_users": media_specs.get('tagged_users', []),
                "caption": post.caption if post.caption else "",
                "downloads": {
                    "Thumbnail": post.url,
                    "Image URL": post.url if not post.is_video else None,
                    "Video URL": post.video_url if post.is_video else None
                }
            }
            
            # Clean up Nones in downloads
            if response['downloads']['Image URL'] is None: del response['downloads']['Image URL']
            if response['downloads']['Video URL'] is None: del response['downloads']['Video URL']

            return jsonify(response), 200

        else:
            return jsonify({"status": "Error", "message": "Missing parameters"}), 400

    except Exception as e:
        return jsonify({"status": "Error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)