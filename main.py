import os
import re
import json
import requests
import anthropic
import firebase_admin
from firebase_admin import credentials, firestore as fb_fs
from datetime import datetime, date
from flask import Flask, send_file, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'sourcing-dev-key')

NAVER_CLIENT_ID     = os.environ.get('NAVER_CLIENT_ID')
NAVER_CLIENT_SECRET = os.environ.get('NAVER_CLIENT_SECRET')
ANTHROPIC_API_KEY   = os.environ.get('ANTHROPIC_API_KEY')
DAILY_LIMIT         = 25_000

# 사용자 목록: APP_USERS_JSON = {"alice": "pass1", "bob": "pass2"}
try:
    APP_USERS = json.loads(os.environ.get('APP_USERS_JSON', '{}'))
except Exception:
    APP_USERS = {}

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Firebase 초기화 ────────────────────────────────────────────────────────
FIREBASE_ENABLED = False
db = None

def init_firebase():
    global FIREBASE_ENABLED, db
    sa_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON')
    if not sa_json:
        print("[Firebase] FIREBASE_SERVICE_ACCOUNT_JSON 미설정 → Firestore 기능 비활성화")
        return
    try:
        sa_dict = json.loads(sa_json)
        cred = credentials.Certificate(sa_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        db = fb_fs.client()
        FIREBASE_ENABLED = True
        print("[Firebase] 초기화 성공")
    except Exception as e:
        print(f"[Firebase] 초기화 실패: {e}")

init_firebase()

# ── Firebase 헬퍼 ─────────────────────────────────────────────────────────
def _today() -> str:
    return date.today().isoformat()

def increment_usage(count: int = 1):
    if not FIREBASE_ENABLED or count == 0:
        return
    try:
        db.collection('api_usage').document(_today()).set(
            {'count': fb_fs.Increment(count), 'date': _today()}, merge=True
        )
    except Exception as e:
        print(f"[Firebase] usage 저장 실패: {e}")

def _progress_key(keyword: str, source: str) -> str:
    """Firestore 문서 ID용 안전한 키 생성"""
    safe = re.sub(r'[^a-zA-Z0-9가-힣]', '_', keyword)[:30]
    return f"{safe}__{source}"

def save_page_progress(keyword: str, source: str, page: int):
    if not FIREBASE_ENABLED:
        return
    try:
        key = _progress_key(keyword, source)
        ref = db.collection('search_progress').document(key)
        doc = ref.get()
        completed = doc.to_dict().get('completed_pages', []) if doc.exists else []
        if page not in completed:
            completed.append(page)
            completed.sort()
        ref.set({
            'keyword': keyword,
            'source': source,
            'completed_pages': completed,
            'last_updated': fb_fs.SERVER_TIMESTAMP,
        }, merge=True)
    except Exception as e:
        print(f"[Firebase] progress 저장 실패: {e}")

# ── 공통 유틸 ─────────────────────────────────────────────────────────────
def strip_html(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text)

def parse_item_date(item: dict) -> Optional[date]:
    if 'postdate' in item:
        try:
            return datetime.strptime(item['postdate'], '%Y%m%d').date()
        except Exception:
            return None
    if 'datetime' in item:
        try:
            return datetime.fromisoformat(item['datetime']).date()
        except Exception:
            return None
    return None

def format_date(d: Optional[date]) -> str:
    return d.strftime('%Y-%m-%d') if d else ''

def naver_search(endpoint: str, query: str, display: int, start: int, headers: dict) -> tuple[list, int]:
    try:
        resp = requests.get(
            f"https://openapi.naver.com/v1/search/{endpoint}.json",
            headers=headers,
            params={"query": query, "display": display, "start": start, "sort": "date"},
            timeout=10,
        )
        if resp.status_code != 200:
            return [], 0
        data = resp.json()
        return data.get("items", []), int(data.get("total", 0))
    except requests.RequestException:
        return [], 0

def analyze_image_for_product(image_url: str) -> dict:
    """썸네일 이미지에서 영문 브랜드명/제품명 추출 (Claude Vision)"""
    import base64
    try:
        resp = requests.get(image_url, timeout=5, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
        if resp.status_code != 200:
            return {}
        content_type = resp.headers.get('content-type', 'image/jpeg').split(';')[0].strip()
        if content_type not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
            content_type = 'image/jpeg'
        image_data = base64.b64encode(resp.content).decode()
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": image_data}},
                    {"type": "text", "text": 'What brand and official English product name/model number is shown in this image? Reply ONLY with JSON: {"brand_name": "...", "product_name_en": "..."}. Use null if not clearly visible.'},
                ],
            }],
        )
        text = response.content[0].text.strip()
        text = re.sub(r'```json?\s*|\s*```', '', text).strip()
        return json.loads(text)
    except Exception as e:
        print(f"[Vision] 분석 실패: {e}")
        return {}


def search_naver_shopping(query: str, headers: dict, brand_name: str = None) -> Optional[dict]:
    if not query.strip():
        return None
    try:
        resp = requests.get(
            "https://openapi.naver.com/v1/search/shop.json",
            headers=headers,
            params={"query": query, "display": 5, "sort": "asc"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        items = resp.json().get("items", [])
        if not items:
            return None

        # 브랜드명이 있으면 결과 중 브랜드명이 제목에 포함된 항목만 사용
        selected = None
        if brand_name:
            brand_lower = brand_name.lower()
            for it in items:
                title = strip_html(it.get("title", "")).lower()
                if brand_lower in title:
                    selected = it
                    break
            if selected is None:
                return None  # 브랜드 불일치 → 엉뚱한 제품 반환 방지
        else:
            selected = items[0]

        return {
            "shopping_price": int(selected.get("lprice", 0)),
            "shopping_image": strip_html(selected.get("image", "")),
            "shopping_link":  selected.get("link", ""),
            "shopping_mall":  strip_html(selected.get("mallName", "")),
            "shopping_title": strip_html(selected.get("title", "")),
        }
    except Exception:
        return None

# ── Pydantic 스키마 ───────────────────────────────────────────────────────
class ReviewItem(BaseModel):
    index: int
    brand_name:                Optional[str] = None
    product_name:              Optional[str] = None
    product_name_en:           Optional[str] = None   # 영문 공식 제품명/모델번호
    category:                  Optional[str] = None
    purchase_source:           Optional[str] = None
    price_paid:                Optional[str] = None   # 후기에서 언급된 구매 가격
    is_direct_purchase_review: Optional[bool] = None  # 실제 해외직구 후기 여부

class ExtractionResult(BaseModel):
    items: List[ReviewItem]

# ── 라우트 ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file('src/index.html')

# 로그인
@app.route("/api/me")
def api_me():
    user = session.get('user')
    return jsonify({"user": user, "logged_in": bool(user)})

@app.route("/api/register", methods=["POST"])
def api_register():
    if not FIREBASE_ENABLED:
        return jsonify({"error": "Firebase 미설정 — 회원가입 불가"}), 503
    data     = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password:
        return jsonify({"error": "사용자명과 비밀번호를 입력해주세요."}), 400
    if len(username) < 2:
        return jsonify({"error": "사용자명은 2자 이상이어야 합니다."}), 400
    if len(password) < 6:
        return jsonify({"error": "비밀번호는 6자 이상이어야 합니다."}), 400
    try:
        ref = db.collection('users').document(username)
        if ref.get().exists:
            return jsonify({"error": "이미 사용 중인 사용자명입니다."}), 409
        ref.set({
            'username':      username,
            'password_hash': generate_password_hash(password),
            'created_at':    fb_fs.SERVER_TIMESTAMP,
        })
        session['user'] = username
        return jsonify({"success": True, "user": username})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/login", methods=["POST"])
def api_login():
    data     = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password:
        return jsonify({"error": "사용자명과 비밀번호를 입력해주세요."}), 400

    # 1) Firestore 사용자 우선 확인
    if FIREBASE_ENABLED:
        try:
            doc = db.collection('users').document(username).get()
            if doc.exists:
                user_data = doc.to_dict()
                if check_password_hash(user_data.get('password_hash', ''), password):
                    session['user'] = username
                    return jsonify({"success": True, "user": username})
                return jsonify({"error": "비밀번호가 틀렸습니다."}), 401
        except Exception as e:
            return jsonify({"error": f"DB 오류: {e}"}), 500

    # 2) 환경변수 APP_USERS_JSON 폴백
    if APP_USERS.get(username) == password:
        session['user'] = username
        return jsonify({"success": True, "user": username})

    return jsonify({"error": "아이디 또는 비밀번호가 틀렸습니다."}), 401

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop('user', None)
    return jsonify({"success": True})

# API 사용량 조회
@app.route("/api/usage")
def get_usage():
    if not FIREBASE_ENABLED:
        return jsonify({"count": 0, "limit": DAILY_LIMIT, "firebase": False})
    try:
        doc = db.collection('api_usage').document(_today()).get()
        count = int(doc.to_dict().get('count', 0)) if doc.exists else 0
        return jsonify({"count": count, "limit": DAILY_LIMIT, "firebase": True})
    except Exception as e:
        return jsonify({"count": 0, "limit": DAILY_LIMIT, "error": str(e)})

# 진행상황 조회
@app.route("/api/progress")
def get_progress():
    keyword = request.args.get('keyword', '').strip()
    source  = request.args.get('source', 'blog')
    if not keyword or not FIREBASE_ENABLED:
        return jsonify({"completed_pages": []})
    try:
        key = _progress_key(keyword, source)
        doc = db.collection('search_progress').document(key).get()
        completed = doc.to_dict().get('completed_pages', []) if doc.exists else []
        return jsonify({"completed_pages": completed})
    except Exception as e:
        return jsonify({"completed_pages": [], "error": str(e)})

# ── 연도 probe (display=1, 총 갯수만) ────────────────────────────────────
@app.route("/api/probe-year")
def probe_year():
    keyword = request.args.get('keyword', '').strip()
    source  = request.args.get('source', 'blog')
    year    = request.args.get('year', '')
    if not keyword or not year:
        return jsonify({"total": 0}), 400
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return jsonify({"total": 0}), 500

    naver_headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    query    = f"{keyword} 직구 후기 {year}년"
    endpoint = "blog" if source == "blog" else "cafearticle"
    _, total = naver_search(endpoint, query, 1, 1, naver_headers)
    increment_usage(1)
    return jsonify({"total": total})


# ── 프로젝트 CRUD ─────────────────────────────────────────────────────────
@app.route("/api/projects", methods=["GET"])
def get_projects():
    if not FIREBASE_ENABLED:
        return jsonify({"items": [], "firebase": False})
    user = session.get('user')
    if not user:
        return jsonify({"error": "로그인 필요"}), 401
    try:
        docs = db.collection('projects').where('user_id', '==', user).stream()
        items = []
        for doc in docs:
            d = doc.to_dict()
            d['id'] = doc.id
            if 'created_at' in d and hasattr(d['created_at'], 'isoformat'):
                d['created_at'] = d['created_at'].isoformat()
            items.append(d)
        items.sort(key=lambda x: x.get('created_at') or '', reverse=True)
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects", methods=["POST"])
def create_project():
    if not FIREBASE_ENABLED:
        return jsonify({"error": "Firebase 미설정"}), 503
    user = session.get('user')
    if not user:
        return jsonify({"error": "로그인 필요"}), 401
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"error": "프로젝트 이름을 입력해주세요."}), 400

    keyword = name
    search_query = f"{keyword} 직구 후기"
    naver_headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }

    # 연도별 검색 방식이므로 총 갯수 확인만 (2 calls)
    blog_first, total_blog = naver_search("blog",        search_query, 5, 1, naver_headers)
    cafe_first, total_cafe = naver_search("cafearticle", search_query, 5, 1, naver_headers)
    increment_usage(2)

    try:
        ref = db.collection('projects').document()
        ref.set({
            'name':       name,
            'keyword':    keyword,
            'user_id':    user,
            'total_blog': total_blog,
            'total_cafe': total_cafe,
            'created_at': fb_fs.SERVER_TIMESTAMP,
        })
        doc = ref.get()
        d = doc.to_dict()
        d['id'] = doc.id
        if 'created_at' in d and hasattr(d['created_at'], 'isoformat'):
            d['created_at'] = d['created_at'].isoformat()
        return jsonify({"project": d})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/<project_id>/history", methods=["PATCH"])
def update_project_history(project_id):
    if not FIREBASE_ENABLED:
        return jsonify({"ok": False})
    user = session.get('user')
    if not user:
        return jsonify({"error": "로그인 필요"}), 401
    data = request.get_json()
    # data: {source: "blog", year: 2023, months: [10, 11, 12]}
    source = data.get('source')
    year   = str(data.get('year'))
    months = data.get('months', [])
    if not source or not year or not months:
        return jsonify({"ok": False}), 400
    try:
        ref = db.collection('projects').document(project_id)
        doc = ref.get()
        if not doc.exists or doc.to_dict().get('user_id') != user:
            return jsonify({"error": "권한 없음"}), 403
        history = doc.to_dict().get('history', {})
        src_hist = history.get(source, {})
        existing = set(src_hist.get(year, []))
        existing.update(months)
        src_hist[year] = sorted(existing)
        history[source] = src_hist
        ref.update({'history': history})
        return jsonify({"ok": True, "history": history})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    if not FIREBASE_ENABLED:
        return jsonify({"error": "Firebase 미설정"}), 503
    user = session.get('user')
    if not user:
        return jsonify({"error": "로그인 필요"}), 401
    try:
        ref = db.collection('projects').document(project_id)
        doc = ref.get()
        if not doc.exists:
            return jsonify({"error": "프로젝트 없음"}), 404
        if doc.to_dict().get('user_id') != user:
            return jsonify({"error": "권한 없음"}), 403
        ref.delete()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 선택 항목 저장 (네이버 쇼핑 조회 포함)
@app.route("/api/save-selected", methods=["POST"])
def save_selected():
    if not FIREBASE_ENABLED:
        return jsonify({
            "error": "Firebase 미설정 상태입니다. Railway Variables에 FIREBASE_SERVICE_ACCOUNT_JSON을 추가해주세요."
        }), 503
    data    = request.get_json()
    items   = data.get('items', [])
    keyword = data.get('keyword', '')
    mode    = data.get('mode', 'review')
    if not items:
        return jsonify({"error": "선택된 항목이 없습니다."}), 400

    naver_headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }

    # ① Vision 분석으로 영문 제품명 보강 (썸네일 있고 영문명 없는 항목만)
    for item in items:
        if item.get('product_name_en'):
            continue
        thumbnail = item.get('thumbnail') or ''
        if thumbnail and thumbnail.startswith('http'):
            vision = analyze_image_for_product(thumbnail)
            if vision.get('product_name_en'):
                item['product_name_en'] = vision['product_name_en']
            if vision.get('brand_name') and not item.get('brand_name'):
                item['brand_name'] = vision['brand_name']

    # ② 영문명 우선으로 네이버 쇼핑 조회
    shopping_cache = {}
    shopping_calls = 0
    for item in items:
        brand   = item.get('brand_name')
        product = item.get('product_name_en') or item.get('product_name')
        parts   = [p for p in [brand, product] if p]
        if not parts:
            continue
        key = " ".join(parts)
        if key not in shopping_cache:
            shopping_cache[key] = search_naver_shopping(key, naver_headers, brand_name=brand)
            shopping_calls += 1
    increment_usage(shopping_calls)

    try:
        batch = db.batch()
        for item in items:
            brand   = item.get('brand_name')
            product = item.get('product_name_en') or item.get('product_name')
            parts   = [p for p in [brand, product] if p]
            shopping_info = shopping_cache.get(" ".join(parts)) if parts else None
            ref = db.collection('sourcing_candidates').document()
            batch.set(ref, {
                **item,
                'keyword':        keyword,
                'saved_at':       fb_fs.SERVER_TIMESTAMP,
                'status':         'pending',
                'saved_by':       session.get('user', 'anonymous'),
                'mode':           mode,
                'blog_image':     item.get('thumbnail') or None,
                'product_name_en': item.get('product_name_en') or None,
                'shopping_price': shopping_info['shopping_price'] if shopping_info else None,
                'shopping_image': shopping_info['shopping_image'] if shopping_info else None,
                'shopping_link':  shopping_info['shopping_link']  if shopping_info else None,
                'shopping_mall':  shopping_info['shopping_mall']  if shopping_info else None,
                'shopping_title': shopping_info['shopping_title'] if shopping_info else None,
            })
        batch.commit()
        return jsonify({"saved": len(items), "success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# OG 이미지 프록시
@app.route("/api/og-image")
def og_image():
    url = request.args.get("url", "")
    if not url or not url.startswith("http"):
        return jsonify({"images": []})
    try:
        # 네이버 블로그: blog.naver.com → m.blog.naver.com (iframe 껍데기 우회)
        fetch_url = url
        m_blog = re.match(r'https?://blog\.naver\.com/([^/?#]+)/([0-9]+)', url)
        if m_blog:
            fetch_url = f"https://m.blog.naver.com/{m_blog.group(1)}/{m_blog.group(2)}"

        resp = requests.get(fetch_url, timeout=6, headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": "https://m.blog.naver.com/",
        })
        images = []

        # 1) og:image
        for pattern in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ]:
            for m in re.finditer(pattern, resp.text, re.IGNORECASE):
                img = m.group(1).strip()
                if img and img not in images:
                    images.append(img)

        # 2) 본문 이미지 (네이버 블로그 CDN 도메인)
        for m in re.finditer(
            r'<img[^>]+src=["\']([^"\']*(?:pstatic\.net|blogfiles\.naver\.net)[^"\']*)["\']',
            resp.text, re.IGNORECASE
        ):
            img = m.group(1).strip()
            if img.startswith('//'):
                img = 'https:' + img
            if img and img not in images:
                images.append(img)
            if len(images) >= 6:
                break

        return jsonify({"images": images})
    except Exception:
        return jsonify({"images": []})

# 메인 검색 (cursor 기반 무한스크롤)
@app.route("/api/search", methods=["POST"])
def search():
    data           = request.get_json()
    keyword        = data.get("keyword", "").strip()
    source         = data.get("source", "blog")
    mode           = data.get("mode", "review")
    year_hint      = data.get("year_hint")        # 연도 (int or None)
    start_date_str = data.get("start_date", "")
    end_date_str   = data.get("end_date", "")
    exclude_raw    = data.get("exclude_keywords", "")
    cursor         = max(1, int(data.get("cursor", 1)))

    if not keyword:
        return jsonify({"error": "키워드를 입력해주세요."}), 400
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return jsonify({"error": "네이버 API 키가 설정되지 않았습니다."}), 500

    start_date = end_date = None
    try:
        if start_date_str: start_date = date.fromisoformat(start_date_str)
        if end_date_str:   end_date   = date.fromisoformat(end_date_str)
    except ValueError:
        return jsonify({"error": "날짜 형식 오류 (YYYY-MM-DD)"}), 400

    naver_headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    if mode == "gonggu":
        search_query = f"{keyword} 직구 공구"
        if source == "blog":
            source = "cafe"
    else:
        # year_hint가 있으면 연도를 쿼리에 포함 → 연도별 독립적인 1000개 풀
        if year_hint:
            search_query = f"{keyword} 직구 후기 {year_hint}년"
        else:
            search_query = f"{keyword} 직구 후기"

    MAX_NAVER_PAGE = 10
    DISPLAY        = 100
    TARGET         = 50   # 한 번 응답에 담을 최대 filtered 항목 수

    all_items  = []
    total_blog = total_cafe = 0
    next_cursor = cursor
    has_more    = False
    hit_too_old = False
    api_calls   = 0

    # MAX_PAGES_PER_CALL 없음 → 한 번 서버 호출로 10페이지 전부 스캔 (클라이언트 재호출 최소화)
    while (len(all_items) < TARGET
           and not hit_too_old
           and next_cursor <= MAX_NAVER_PAGE):

        start_pos  = (next_cursor - 1) * DISPLAY + 1
        page_items = []

        if source in ("blog", "all"):
            items, tb = naver_search("blog", search_query, DISPLAY, start_pos, naver_headers)
            for i in items: i["_source"] = "블로그"
            page_items.extend(items)
            if tb: total_blog = tb
            api_calls += 1

        if source in ("cafe", "all"):
            items, tc = naver_search("cafearticle", search_query, DISPLAY, start_pos, naver_headers)
            for i in items: i["_source"] = "카페"
            page_items.extend(items)
            if tc: total_cafe = tc
            api_calls += 1

        if not page_items:
            break

        for item in page_items:
            item["_date"] = parse_item_date(item)
        page_items.sort(key=lambda x: x["_date"] or date.min, reverse=True)

        for item in page_items:
            d = item["_date"]
            if end_date and d and d > end_date:
                continue          # 너무 최신 → skip
            if start_date and d and d < start_date:
                hit_too_old = True
                break             # 범위 이전 → 중단
            all_items.append(item)

        next_cursor += 1

    increment_usage(api_calls)

    if not hit_too_old:
        total = (total_blog if source == "blog"
                 else total_cafe if source == "cafe"
                 else total_blog + total_cafe)
        has_more = next_cursor <= MAX_NAVER_PAGE and (next_cursor - 1) * DISPLAY < total

    # 네이버 검색 스타일 필터:
    # 1) 제목에 keyword 포함 (필수 - 핵심 주제 확인)
    # 2) 제목 또는 내용에 '직구' 포함 (완화 - 내용에만 있어도 OK)
    keyword_lower = keyword.lower()
    all_items = [i for i in all_items
                 if keyword_lower in strip_html(i.get("title", "")).lower()
                 and '직구' in (strip_html(i.get("title", "")) + strip_html(i.get("description", "")))]

    exclude_keywords = [k.strip() for k in exclude_raw.split(',') if k.strip()]
    if exclude_keywords:
        def not_excluded(item):
            text = (strip_html(item.get("title", "")) + " " + strip_html(item.get("description", ""))).lower()
            return not any(kw.lower() in text for kw in exclude_keywords)
        all_items = [i for i in all_items if not_excluded(i)]

    all_items.sort(key=lambda x: x["_date"] or date.min, reverse=True)

    if not all_items:
        return jsonify({
            "results": [], "total_blog": total_blog, "total_cafe": total_cafe,
            "next_cursor": next_cursor, "has_more": has_more, "keyword": keyword,
        })

    # Claude 추출
    reviews_text = ""
    for i, item in enumerate(all_items, 1):
        title       = strip_html(item.get("title", ""))
        description = strip_html(item.get("description", ""))
        reviews_text += f"[{i}] 제목: {title}\n설명: {description}\n\n"

    if mode == "gonggu":
        claude_content = f"""다음은 카페 해외 직구 공구 관련 게시물 목록입니다. 각 게시물에서 정보를 추출해주세요.

추출 항목:
- brand_name: 브랜드명 (예: Nike, Apple, Zara 등, 없으면 null)
- product_name: 구체적인 상품명/모델명 (없으면 null)
- category: 신발/의류/전자제품/가방/화장품/식품/기타 중 하나 (없으면 null)
- purchase_source: 공구 진행 카페명 또는 구매처 (없으면 null)
- price_paid: 공구 가격 또는 구매 가격 (예: "$120", "15만원", 없으면 null)
- is_direct_purchase_review: 실제 공구 모집/진행 게시물이면 true, 단순 언급이나 후기이면 false

게시물 목록:
{reviews_text}
index는 게시물 번호 숫자를 그대로 사용하세요."""
    else:
        claude_content = f"""다음은 해외 직구 관련 후기 목록입니다. 각 후기에서 정보를 추출해주세요.

추출 항목:
- brand_name: 브랜드명 (예: Nike, Apple, Zara 등, 없으면 null)
- product_name: 상품명/모델명 한국어로 (없으면 null)
- product_name_en: 본문에 영문으로 실제 표기된 공식 제품명/모델번호 (한국어를 영어로 번역하지 말 것, 텍스트에 영문이 명시된 경우에만 추출, 없으면 null)
- category: 신발/의류/전자제품/가방/화장품/식품/기타 중 하나 (없으면 null)
- purchase_source: 구매처 (아마존/이베이/알리익스프레스/직접구매/구매대행 등, 없으면 null)
- price_paid: 후기에서 언급된 구매 가격 (예: "$120", "15만원", "89달러", 없으면 null)
- is_direct_purchase_review: 실제로 해외직구(아마존/이베이/알리/직접구매 등)로 구매한 상품의 후기면 true, 단순 브랜드 언급/AS수리안내/광고/국내구매 후기면 false

후기 목록:
{reviews_text}
index는 후기 번호 숫자를 그대로 사용하세요."""

    try:
        response = claude.messages.parse(
            model="claude-haiku-4-5-20251001",
            max_tokens=16000,
            messages=[{"role": "user", "content": claude_content}],
            output_format=ExtractionResult,
        )
    except anthropic.APIError as e:
        return jsonify({"error": f"Claude API 오류: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"분석 처리 오류: {str(e)}"}), 500

    extracted_map = {item.index: item for item in response.parsed_output.items}

    results = []
    for i, item in enumerate(all_items, 1):
        ext = extracted_map.get(i)
        results.append({
            "index":                     i,
            "source":                    item.get("_source", "블로그"),
            "title":                     strip_html(item.get("title", "")),
            "description":               strip_html(item.get("description", "")),
            "link":                      item.get("link", ""),
            "thumbnail":                 item.get("thumbnail", "") or "",
            "author":                    item.get("bloggerName") or item.get("cafename") or "",
            "postdate":                  format_date(item["_date"]),
            "brand_name":                ext.brand_name               if ext else None,
            "product_name":              ext.product_name             if ext else None,
            "product_name_en":           ext.product_name_en          if ext else None,
            "category":                  ext.category                 if ext else None,
            "purchase_source":           ext.purchase_source          if ext else None,
            "price_paid":                ext.price_paid               if ext else None,
            "is_direct_purchase_review": getattr(ext, 'is_direct_purchase_review', True) if ext else True,
        })

    return jsonify({
        "results":     results,
        "total_blog":  total_blog,
        "total_cafe":  total_cafe,
        "next_cursor": next_cursor,
        "has_more":    has_more,
        "keyword":     keyword,
        "mode":        mode,
    })


# 소싱 후보 목록 조회
@app.route("/api/candidates")
def get_candidates():
    if not FIREBASE_ENABLED:
        return jsonify({"items": [], "firebase": False,
                        "error": "Firebase 미설정 — FIREBASE_SERVICE_ACCOUNT_JSON 환경변수를 확인해주세요."})
    try:
        # order_by 없이 전체 조회 후 Python에서 정렬 (Firestore 인덱스 불필요)
        docs = db.collection('sourcing_candidates').limit(500).stream()
        items = []
        for doc in docs:
            d = doc.to_dict()
            d['id'] = doc.id
            # saved_at 직렬화 (Firestore timestamp → ISO string)
            if 'saved_at' in d and hasattr(d['saved_at'], 'isoformat'):
                d['saved_at'] = d['saved_at'].isoformat()
            items.append(d)
        # 최신순 정렬
        items.sort(key=lambda x: x.get('saved_at') or '', reverse=True)
        return jsonify({"items": items, "count": len(items), "firebase": True})
    except Exception as e:
        return jsonify({"items": [], "error": str(e), "firebase": True})


# 소싱 후보 상태 업데이트
ALLOWED_UPDATE_FIELDS = {
    'status', 'price_eur', 'exchange_rate', 'shipping_fee',
    'cost_price', 'margin', 'margin_rate', 'memo',
    'completed_by', 'completed_at',
    'weight_kg', 'vat_type', 'product_url',
}

@app.route("/api/candidates/<doc_id>", methods=["PATCH"])
def update_candidate(doc_id):
    if not FIREBASE_ENABLED:
        return jsonify({"error": "Firebase 미설정"}), 503
    data = request.get_json()
    update_data = {k: v for k, v in data.items() if k in ALLOWED_UPDATE_FIELDS}
    if not update_data:
        return jsonify({"error": "변경할 데이터 없음"}), 400
    try:
        db.collection('sourcing_candidates').document(doc_id).update(update_data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 소싱 후보 삭제
@app.route("/api/candidates/<doc_id>", methods=["DELETE"])
def delete_candidate(doc_id):
    if not FIREBASE_ENABLED:
        return jsonify({"error": "Firebase 미설정"}), 503
    try:
        db.collection('sourcing_candidates').document(doc_id).delete()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/exchange-rate")
def exchange_rate():
    """EUR/KRW 최신 환율 조회"""
    try:
        resp = requests.get("https://api.frankfurter.app/latest?base=EUR&symbols=KRW", timeout=5)
        data = resp.json()
        rate = round(data['rates']['KRW'])
        return jsonify({"rate": rate, "base": "EUR", "target": "KRW"})
    except Exception as e:
        return jsonify({"rate": 1450, "error": str(e)})


@app.route("/api/generate-product-name", methods=["POST"])
def generate_product_name():
    """SEO 최적화 등록상품명 생성"""
    data           = request.get_json()
    brand          = data.get('brand_name', '')
    product        = data.get('product_name', '')
    product_en     = data.get('product_name_en', '')
    category       = data.get('category', '')
    country        = data.get('country', '')
    product_url    = data.get('product_url', '')
    review_title   = data.get('review_title', '')      # 블로그/카페 후기 제목
    review_desc    = data.get('review_description', '') # 후기 본문 발췌

    prompt = f"""다음 제품의 네이버 스마트스토어/쿠팡 SEO 최적화 상품명을 50byte 버전과 100byte 버전으로 각각 작성해주세요.

제품 정보:
- 브랜드/제조사: {brand}
- 제품명(한국어): {product}
- 제품명(영문): {product_en}
- 카테고리: {category}
- 소싱국가: {country}
- 소싱처 URL: {product_url}

실제 구매 후기 제목 (소비자 검색 키워드 참고):
{review_title}

후기 본문 발췌 (소비자 표현/키워드 참고):
{review_desc[:300] if review_desc else '없음'}

상품명 구성 순서 (해당 정보가 있을 때만 포함):
브랜드/제조사 → 시리즈 → 모델명 → 상품유형 → 색상 → 소재 → 수량(갯수묶음) → 사이즈 → 성별 → 속성(Spec/용량/무게/연식/호수 등)

바이트 계산: 한글 1자=3byte, 영문·숫자·공백 1자=1byte

작성 규칙:
- URL·제품명에서 추출 가능한 스펙(색상, 용량ml/g, 사이즈, 갯수, 소재, 모델번호, 빈티지연도 등) 최대한 포함
- 후기 제목의 소비자 검색 키워드를 자연스럽게 포함
- 특수문자 최소화 (네이버/쿠팡 등록 허용 문자만)

반드시 아래 형식으로만 반환 (설명·이유 없이):
50byte: [50바이트 이하 상품명]
100byte: [100바이트 이하, 최대한 꽉 채운 상품명]"""

    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        name_50 = name_100 = ''
        for line in raw.splitlines():
            if line.startswith('50byte:'):
                name_50  = line[len('50byte:'):].strip()
            elif line.startswith('100byte:'):
                name_100 = line[len('100byte:'):].strip()
        # fallback: 파싱 실패 시 전체 텍스트를 100byte로
        if not name_100:
            name_100 = raw.split('\n')[0].strip()
        if not name_50:
            name_50  = name_100[:50]
        return jsonify({"name_50": name_50, "name_100": name_100})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


ALLOWED_REG_FIELDS = {
    'product_name_display', 'name_50', 'name_100',
    'naver_price', 'coupang_price',
    'customs_rate', 'fta', 'fta_agreement', 'status', 'memo', 'country',
}

@app.route("/api/product-registrations", methods=["GET"])
def get_product_registrations():
    if not FIREBASE_ENABLED:
        return jsonify({"items": [], "firebase": False})
    try:
        docs = db.collection('product_registrations').limit(500).stream()
        items = []
        for doc in docs:
            d = doc.to_dict(); d['id'] = doc.id
            if 'created_at' in d and hasattr(d['created_at'], 'isoformat'):
                d['created_at'] = d['created_at'].isoformat()
            items.append(d)
        items.sort(key=lambda x: x.get('created_at') or '', reverse=True)
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"items": [], "error": str(e)})


@app.route("/api/product-registrations", methods=["POST"])
def create_product_registration():
    if not FIREBASE_ENABLED:
        return jsonify({"error": "Firebase 미설정"}), 503
    items = request.get_json().get('items', [])
    if not items:
        return jsonify({"error": "항목 없음"}), 400
    try:
        batch = db.batch()
        for item in items:
            ref = db.collection('product_registrations').document()
            batch.set(ref, {**item, 'created_at': fb_fs.SERVER_TIMESTAMP,
                            'created_by': session.get('user', 'anonymous'), 'status': 'draft'})
        batch.commit()
        return jsonify({"created": len(items)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/product-registrations/<doc_id>", methods=["PATCH"])
def update_product_registration(doc_id):
    if not FIREBASE_ENABLED:
        return jsonify({"error": "Firebase 미설정"}), 503
    data = {k: v for k, v in request.get_json().items() if k in ALLOWED_REG_FIELDS}
    if not data:
        return jsonify({"error": "변경할 데이터 없음"}), 400
    try:
        db.collection('product_registrations').document(doc_id).update(data)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/product-registrations/<doc_id>", methods=["DELETE"])
def delete_product_registration(doc_id):
    if not FIREBASE_ENABLED:
        return jsonify({"error": "Firebase 미설정"}), 503
    try:
        db.collection('product_registrations').document(doc_id).delete()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/fetch-weight", methods=["POST"])
def fetch_weight():
    """Amazon.de / idealo 제품 페이지에서 무게 추출"""
    url = request.get_json().get('url', '').strip()
    if not url or not url.startswith('http'):
        return jsonify({"weight": None, "error": "유효한 URL이 아닙니다"}), 400
    try:
        resp = requests.get(url, timeout=8, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml",
        })
        if resp.status_code != 200:
            return jsonify({"weight": None, "error": f"페이지 로드 실패 ({resp.status_code})"}), 400

        text = strip_html(resp.text)

        # 정규식으로 무게 패턴 찾기
        weight_patterns = [
            (r'(?:Artikelgewicht|Item\s+Weight|Gewicht|Versandgewicht|Stückgewicht)[^\d]{0,30}([0-9]+[.,][0-9]*)\s*(kg|Kilogramm)', 'kg'),
            (r'(?:Artikelgewicht|Item\s+Weight|Gewicht|Versandgewicht|Stückgewicht)[^\d]{0,30}([0-9]+)\s*(kg|Kilogramm)', 'kg'),
            (r'(?:Artikelgewicht|Item\s+Weight|Gewicht|Versandgewicht|Stückgewicht)[^\d]{0,30}([0-9]+[.,][0-9]*)\s*(g|Gramm)\b', 'g'),
            (r'(?:Artikelgewicht|Item\s+Weight|Gewicht|Versandgewicht|Stückgewicht)[^\d]{0,30}([0-9]+)\s*(g|Gramm)\b', 'g'),
        ]
        found_weight = None
        for pattern, default_unit in weight_patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                val = float(m.group(1).replace(',', '.'))
                if default_unit == 'g':
                    val = val / 1000
                found_weight = round(val, 3)
                break

        # 가격 정규식 (EUR)
        found_price = None
        price_patterns = [
            r'([0-9]+[.,][0-9]{2})\s*€',
            r'€\s*([0-9]+[.,][0-9]{2})',
            r'EUR\s*([0-9]+[.,][0-9]{2})',
            r'Preis[^\d]{0,20}([0-9]+[.,][0-9]{2})',
        ]
        for pp in price_patterns:
            pm = re.search(pp, text)
            if pm:
                found_price = round(float(pm.group(1).replace(',', '.')), 2)
                break

        if found_weight is not None and found_price is not None:
            return jsonify({"weight": found_weight, "price_eur": found_price, "unit": "kg", "source": "regex"})
        if found_weight is not None:
            pass  # 가격은 Claude로 보완 시도

        # Claude로 무게+가격 추출 (정규식에서 못 찾은 필드 보완)
        relevant_text = text[:4000]
        for kw in ['Gewicht', 'Weight', 'Artikelgewicht', 'Preis', 'Price', '€', 'kg']:
            idx = text.find(kw)
            if idx > 200:
                relevant_text = text[max(0, idx - 300): idx + 800]
                break

        resp_claude = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content":
                f'Extract product weight and price from this text. '
                f'Reply ONLY with JSON: {{"weight": 0.5, "unit": "kg", "price_eur": 29.99}}. '
                f'Weight in kg (convert grams to kg). Price in EUR as number only. '
                f'Use null for any field not found.\n\n{relevant_text}'
            }],
        )
        result_text = re.sub(r'```json?\s*|\s*```', '', resp_claude.content[0].text.strip()).strip()
        result = json.loads(result_text)
        out = {}
        if found_weight is not None:
            out['weight'] = found_weight
        elif result.get('weight') is not None:
            w = float(result['weight'])
            if result.get('unit', 'kg') in ('g', 'gram', 'Gramm'):
                w = w / 1000
            out['weight'] = round(w, 3)
        if found_price is not None:
            out['price_eur'] = found_price
        elif result.get('price_eur') is not None:
            out['price_eur'] = round(float(result['price_eur']), 2)
        if out:
            out['source'] = 'claude'
            return jsonify(out)

        return jsonify({"weight": None, "error": "무게/가격 정보를 찾을 수 없습니다. 직접 입력해주세요."})
    except Exception as e:
        return jsonify({"weight": None, "error": str(e)}), 500


def main():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 80)))


if __name__ == "__main__":
    main()
