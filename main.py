import os
import re
import json
import requests
import anthropic
import firebase_admin
from firebase_admin import credentials, firestore as fb_fs
from datetime import datetime, date
from flask import Flask, send_file, request, jsonify
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

NAVER_CLIENT_ID     = os.environ.get('NAVER_CLIENT_ID')
NAVER_CLIENT_SECRET = os.environ.get('NAVER_CLIENT_SECRET')
ANTHROPIC_API_KEY   = os.environ.get('ANTHROPIC_API_KEY')
DAILY_LIMIT         = 25_000

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

def search_naver_shopping(query: str, headers: dict) -> Optional[dict]:
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
        item = items[0]
        return {
            "shopping_price": int(item.get("lprice", 0)),
            "shopping_image": strip_html(item.get("image", "")),
            "shopping_link":  item.get("link", ""),
            "shopping_mall":  strip_html(item.get("mallName", "")),
        }
    except Exception:
        return None

# ── Pydantic 스키마 ───────────────────────────────────────────────────────
class ReviewItem(BaseModel):
    index: int
    brand_name:                Optional[str] = None
    product_name:              Optional[str] = None
    category:                  Optional[str] = None
    purchase_source:           Optional[str] = None
    price_paid:                Optional[str] = None   # 후기에서 언급된 구매 가격
    is_direct_purchase_review: bool = True            # 실제 해외직구 후기 여부

class ExtractionResult(BaseModel):
    items: List[ReviewItem]

# ── 라우트 ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file('src/index.html')

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
    if not items:
        return jsonify({"error": "선택된 항목이 없습니다."}), 400

    # 선택된 항목만 네이버 쇼핑 조회
    naver_headers = {
        "X-Naver-Client-Id":     NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    shopping_cache = {}
    shopping_calls = 0
    for item in items:
        parts = [p for p in [item.get('brand_name'), item.get('product_name')] if p]
        if not parts:
            continue
        key = " ".join(parts)
        if key not in shopping_cache:
            shopping_cache[key] = search_naver_shopping(key, naver_headers)
            shopping_calls += 1
    increment_usage(shopping_calls)

    try:
        batch = db.batch()
        for item in items:
            parts = [p for p in [item.get('brand_name'), item.get('product_name')] if p]
            shopping_info = shopping_cache.get(" ".join(parts)) if parts else None
            ref = db.collection('sourcing_candidates').document()
            batch.set(ref, {
                **item,
                'keyword':        keyword,
                'saved_at':       fb_fs.SERVER_TIMESTAMP,
                'status':         'pending',
                'shopping_price': shopping_info['shopping_price'] if shopping_info else None,
                'shopping_image': shopping_info['shopping_image'] if shopping_info else None,
                'shopping_link':  shopping_info['shopping_link']  if shopping_info else None,
                'shopping_mall':  shopping_info['shopping_mall']  if shopping_info else None,
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
        resp = requests.get(url, timeout=5, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9",
        })
        images = []
        for pattern in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        ]:
            for m in re.finditer(pattern, resp.text, re.IGNORECASE):
                img = m.group(1).strip()
                if img and img not in images:
                    images.append(img)
                if len(images) >= 2:
                    break
            if images:
                break
        return jsonify({"images": images})
    except Exception:
        return jsonify({"images": []})

# 메인 검색
@app.route("/api/search", methods=["POST"])
def search():
    data            = request.get_json()
    keyword         = data.get("keyword", "").strip()
    page            = max(1, min(10, int(data.get("page", 1))))
    source          = data.get("source", "blog")
    start_date_str  = data.get("start_date", "")
    end_date_str    = data.get("end_date", "")
    exclude_raw     = data.get("exclude_keywords", "")

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
    search_query = f"{keyword} 직구 후기"

    # 페이지별 start 계산 (소스별 100개 or "all"이면 각 50개)
    if source == "all":
        display_per = 50
    else:
        display_per = 100
    start_pos = (page - 1) * display_per + 1

    all_items   = []
    total_blog  = total_cafe = 0
    api_calls   = 0

    if source in ("blog", "all"):
        blog_items, total_blog = naver_search("blog", search_query, display_per, start_pos, naver_headers)
        for item in blog_items:
            item["_source"] = "블로그"
        all_items.extend(blog_items)
        api_calls += 1

    if source in ("cafe", "all"):
        cafe_items, total_cafe = naver_search("cafearticle", search_query, display_per, start_pos, naver_headers)
        for item in cafe_items:
            item["_source"] = "카페"
        all_items.extend(cafe_items)
        api_calls += 1

    increment_usage(api_calls)

    # 날짜 파싱 & 필터링
    for item in all_items:
        item["_date"] = parse_item_date(item)

    exclude_keywords = [k.strip() for k in exclude_raw.split(',') if k.strip()]

    if start_date or end_date:
        def in_range(item):
            d = item["_date"]
            if d is None: return False
            if start_date and d < start_date: return False
            if end_date   and d > end_date:   return False
            return True
        all_items = [i for i in all_items if in_range(i)]

    if exclude_keywords:
        def not_excluded(item):
            text = (strip_html(item.get("title", "")) + " " + strip_html(item.get("description", ""))).lower()
            return not any(kw.lower() in text for kw in exclude_keywords)
        all_items = [i for i in all_items if not_excluded(i)]

    all_items.sort(key=lambda x: x["_date"] or date.min, reverse=True)

    # 진행상황 저장 (결과 없어도 방문한 것으로 저장)
    save_page_progress(keyword, source, page)

    if not all_items:
        return jsonify({
            "results": [], "total_blog": total_blog, "total_cafe": total_cafe,
            "keyword": keyword, "page": page,
        })

    # Claude 추출
    reviews_text = ""
    for i, item in enumerate(all_items, 1):
        title       = strip_html(item.get("title", ""))
        description = strip_html(item.get("description", ""))
        reviews_text += f"[{i}] 제목: {title}\n설명: {description}\n\n"

    try:
        response = claude.messages.parse(
            model="claude-opus-4-6",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": f"""다음은 해외 직구 관련 후기 목록입니다. 각 후기에서 정보를 추출해주세요.

추출 항목:
- brand_name: 브랜드명 (예: Nike, Apple, Zara 등, 없으면 null)
- product_name: 구체적인 상품명/모델명 (없으면 null)
- category: 신발/의류/전자제품/가방/화장품/식품/기타 중 하나 (없으면 null)
- purchase_source: 구매처 (아마존/이베이/알리익스프레스/직접구매/구매대행 등, 없으면 null)
- price_paid: 후기에서 언급된 구매 가격 (예: "$120", "15만원", "89달러", 없으면 null)
- is_direct_purchase_review: 실제로 해외직구(아마존/이베이/알리/직접구매 등)로 구매한 상품의 후기면 true, 단순 브랜드 언급/AS수리안내/광고/국내구매 후기면 false

후기 목록:
{reviews_text}
index는 후기 번호 숫자를 그대로 사용하세요."""
            }],
            output_format=ExtractionResult,
        )
    except anthropic.APIError as e:
        return jsonify({"error": f"Claude API 오류: {str(e)}"}), 500

    extracted_map = {item.index: item for item in response.parsed_output.items}

    # 쇼핑 조회는 "다음 단계로" 버튼 클릭 시 선택 항목만 조회 (save-selected 엔드포인트)
    results = []
    for i, item in enumerate(all_items, 1):
        ext = extracted_map.get(i)
        results.append({
            "index":                    i,
            "source":                   item.get("_source", "블로그"),
            "title":                    strip_html(item.get("title", "")),
            "description":              strip_html(item.get("description", "")),
            "link":                     item.get("link", ""),
            "author":                   item.get("bloggerName") or item.get("cafename") or "",
            "postdate":                 format_date(item["_date"]),
            "brand_name":               ext.brand_name                if ext else None,
            "product_name":             ext.product_name              if ext else None,
            "category":                 ext.category                  if ext else None,
            "purchase_source":          ext.purchase_source           if ext else None,
            "price_paid":               ext.price_paid                if ext else None,
            "is_direct_purchase_review": getattr(ext, 'is_direct_purchase_review', True) if ext else True,
        })

    return jsonify({
        "results":     results,
        "total_blog":  total_blog,
        "total_cafe":  total_cafe,
        "keyword":     keyword,
        "page":        page,
    })


# 소싱 후보 목록 조회
@app.route("/api/candidates")
def get_candidates():
    if not FIREBASE_ENABLED:
        return jsonify({"items": [], "firebase": False})
    try:
        docs = (db.collection('sourcing_candidates')
                .order_by('saved_at', direction=fb_fs.Query.DESCENDING)
                .limit(500)
                .stream())
        items = []
        for doc in docs:
            d = doc.to_dict()
            d['id'] = doc.id
            if 'saved_at' in d and hasattr(d['saved_at'], 'isoformat'):
                d['saved_at'] = d['saved_at'].isoformat()
            items.append(d)
        return jsonify({"items": items, "count": len(items)})
    except Exception as e:
        return jsonify({"items": [], "error": str(e)})


# 소싱 후보 상태 업데이트
@app.route("/api/candidates/<doc_id>", methods=["PATCH"])
def update_candidate(doc_id):
    if not FIREBASE_ENABLED:
        return jsonify({"error": "Firebase 미설정"}), 503
    data = request.get_json()
    update_data = {}
    if 'status' in data:
        update_data['status'] = data['status']
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


def main():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 80)))


if __name__ == "__main__":
    main()
