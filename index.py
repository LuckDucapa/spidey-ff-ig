from flask import Flask, request, jsonify
from itertools import islice
import instaloader
import re

app = Flask(__name__)

# ==========================================================
#                   HELPER FUNCTIONS
# ==========================================================

def get_shortcode_from_url(url):
    """Extracts shortcode from URL or returns the input if it's already a code."""
    match = re.search(r'instagram\.com/(?:reel|p)/([^/?#&]+)', url)
    if match:
        return match.group(1)
    return url.strip('/')

def safe_int(value):
    """Safely converts to int or returns None."""
    try:
        if value is None: return None
        return int(value)
    except:
        return None

def extract_author_data(profile):
    """Extracts detailed author info from a Profile object."""
    if not profile:
        return None

    # Bio Links extraction
    bio_links = []
    try:
        if hasattr(profile, '_node'):
            bio_links = profile._node.get('bio_links', [])
        if not bio_links and profile.external_url:
            bio_links = [{'title': 'External Link', 'url': profile.external_url}]
    except: pass

    return {
        "username": profile.username,
        "full_name": profile.full_name,
        "id": profile.userid,
        "is_verified": profile.is_verified,
        "is_business": profile.is_business_account,
        "followers": safe_int(profile.followers),
        "following": safe_int(profile.followees),
        "total_posts": safe_int(profile.mediacount),
        "biography": profile.biography,
        "avatar_url": profile.profile_pic_url,
        "bio_links": bio_links
    }

def extract_post_data(post, author_override=None):
    """
    Extracts all details from a Post object.
    Returns the exact JSON structure defined for single posts.
    """
    # 1. Audio / Music Info
    audio_info = None
    try:
        if 'clips_music_attribution_info' in post._node:
            music_node = post._node['clips_music_attribution_info']
            if music_node:
                audio_info = {
                    'song_name': music_node.get('song_name', 'Unknown'),
                    'artist_name': music_node.get('artist_name', 'Unknown'),
                    'is_original': music_node.get('uses_original_audio', False),
                    'audio_id': music_node.get('audio_id', None)
                }
    except: pass

    # 2. Detailed Tagged Users
    tagged_users = []
    try:
        # Deep search via edges to get verification status and full names
        edges = post._node.get('edge_media_to_tagged_user', {}).get('edges', [])
        for edge in edges:
            node = edge.get('node', {}).get('user', {})
            tagged_users.append({
                'username': node.get('username', 'Unknown'),
                'full_name': node.get('full_name', ''),
                'is_verified': node.get('is_verified', False)
            })
    except: pass

    # 3. Views & Plays
    views = post.video_view_count
    if views is None: views = post._node.get('video_view_count')
    if views is None: views = post._node.get('play_count')

    # 4. Dimensions
    width = getattr(post, 'width', None)
    height = getattr(post, 'height', None)
    if not width:
        dims = post._node.get('dimensions', {})
        width = dims.get('width', None)
        height = dims.get('height', None)

    # 5. Author (If not provided by override)
    author_data = author_override
    if not author_data:
        # Minimal author data if we don't have the full profile object loaded
        author_data = {
            "username": post.owner_username,
            "id": post.owner_id,
        }

    return {
        "shortcode": post.shortcode,
        "author": author_data,
        "media_specs": {
            "type": post.typename,
            "width": safe_int(width),
            "height": safe_int(height),
            "duration_seconds": post.video_duration,
            "upload_date": post.date_local.isoformat()
        },
        "audio": audio_info,
        "engagement": {
            "views": safe_int(views),
            "likes": safe_int(post.likes),
            "comments": safe_int(post.comments),
            "tagged_users": tagged_users
        },
        "content": {
            "caption": post.caption,
        },
        "download_links": {
            "thumbnail": post.url,
            "video_url": post.video_url, # None if image
            "image_url": post.url 
        }
    }

# ==========================================================
#                       MAIN ROUTE
# ==========================================================

@app.route('/ig', methods=['GET'])
def instagram_api():
    # 1. Get Arguments
    url = request.args.get('url')
    pid = request.args.get('id')       # Post/Reel ID
    username = request.args.get('username')
    userid = request.args.get('userid') # User ID

    # 2. Initialize Instaloader
    L = instaloader.Instaloader()
    # Spoof User-Agent
    L.context._session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    })

    try:
        # -----------------------------
        # CASE A: FETCH BY PROFILE (Username or UserID)
        # -----------------------------
        if username or userid:
            profile = None
            try:
                if username:
                    profile = instaloader.Profile.from_username(L.context, username)
                elif userid:
                    # Fetching by ID often requires login, but we try anyway
                    profile = instaloader.Profile.from_id(L.context, int(userid))
            except instaloader.ProfileNotExistsException:
                return jsonify({"status": "error", "message": "Profile not found"}), 404
            except instaloader.LoginRequiredException:
                return jsonify({"status": "error", "message": "Login required to view this profile"}), 403
            except Exception as e:
                return jsonify({"status": "error", "message": str(e)}), 500

            # Extract Author Info
            author_full_info = extract_author_data(profile)

            # Fetch Recent 7 Posts
            recent_posts_data = []
            try:
                # islice fetches only the first 7 posts from the iterator
                posts_iterator = profile.get_posts()
                for post in islice(posts_iterator, 7):
                    # We pass 'author_full_info' so we don't re-scrape the author for every post
                    post_data = extract_post_data(post, author_override=author_full_info)
                    recent_posts_data.append(post_data)
            except Exception as e:
                # If posts fail to load (private account), we still return profile info
                pass

            return jsonify({
                "status": "success",
                "type": "profile",
                "author": author_full_info,
                "recent_posts_count": len(recent_posts_data),
                "recent_posts": recent_posts_data
            }), 200

        # -----------------------------
        # CASE B: FETCH BY POST (URL or ID)
        # -----------------------------
        elif url or pid:
            target_input = url if url else pid
            shortcode = get_shortcode_from_url(target_input)
            
            try:
                post = instaloader.Post.from_shortcode(L.context, shortcode)
            except instaloader.ProfileNotExistsException:
                return jsonify({"status": "error", "message": "Post not found"}), 404
            except instaloader.LoginRequiredException:
                return jsonify({"status": "error", "message": "Login required"}), 403

            # Try to fetch full author info (optional)
            author_details = None
            try:
                p = post.owner_profile
                # Force fetch metadata
                _ = p.biography 
                author_details = extract_author_data(p)
            except:
                pass

            # Extract Post Data
            result = extract_post_data(post, author_override=author_details)
            
            # Wrap in status for consistency
            final_response = {
                "status": "success",
                "type": "media",
                **result
            }
            return jsonify(final_response), 200

        else:
            return jsonify({"status": "error", "message": "Missing parameters. Use 'url', 'id', 'username', or 'userid'."}), 400

    except Exception as e:
        return jsonify({"status": "error", "message": f"Server Error: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(debug=True)