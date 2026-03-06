import os
import re
import requests
import anthropic
from datetime import datetime, date
from flask import Flask, send_file, request, jsonify
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

NAVER_CLIENT_ID = os.environ.get('NAVER_CLIENT_ID')
NAVER_CLIENT_SECRET = os.environ.get('NAVER_CLIENT_SECRET')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def strip_html(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text)


def parse_item_date(item: dict) -> Optional[date]:
    """블로그(YYYYMMDD) 또는 카페(ISO) 날짜 파싱"""
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


def naver_search(endpoint: str, query: str, display: int, headers: dict) -> tuple[list, int]:
    """네이버 검색 API 호출. (items, total) 반환"""
    try:
        resp = requests.get(
            f"https://openapi.naver.com/v1/search/{endpoint}.json",
            headers=headers,
            params={"query": query, "display": display, "sort": "date"},
            timeout=10,
        )
        if resp.status_code != 200:
            return [], 0
        data = resp.json()
        return data.get("items", []), int(data.get("total", 0))
    except requests.RequestException:
        return [], 0


class ReviewItem(BaseModel):
    index: int
    brand_name: Optional[str] = None
    product_name: Optional[str] = None
    category: Optional[str] = None
    purchase_source: Optional[str] = None


class ExtractionResult(BaseModel):
    items: List[ReviewItem]


@app.route("/")
def index():
    return send_file('src/index.html')


@app.route("/api/search", methods=["POST"])
def search():
    data = request.get_json()
    keyword = data.get("keyword", "").strip()
    display = min(int(data.get("display", 10)), 30)
    source = data.get("source", "blog")          # blog | cafe | all
    start_date_str = data.get("start_date", "")  # YYYY-MM-DD
    end_date_str = data.get("end_date", "")       # YYYY-MM-DD
    exclude_raw = data.get("exclude_keywords", "") # 쉼표 구분 문자열

    if not keyword:
        return jsonify({"error": "키워드를 입력해주세요."}), 400

    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return jsonify({"error": "네이버 API 키가 설정되지 않았습니다."}), 500

    # 날짜 범위 파싱
    start_date = None
    end_date = None
    try:
        if start_date_str:
            start_date = date.fromisoformat(start_date_str)
        if end_date_str:
            end_date = date.fromisoformat(end_date_str)
    except ValueError:
        return jsonify({"error": "날짜 형식이 올바르지 않습니다. (YYYY-MM-DD)"}), 400

    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    search_query = f"{keyword} 직구 후기"

    # 검색 실행
    all_items = []
    total_blog = total_cafe = 0

    if source in ("blog", "all"):
        blog_items, total_blog = naver_search("blog", search_query, display, headers)
        for item in blog_items:
            item["_source"] = "블로그"
        all_items.extend(blog_items)

    if source in ("cafe", "all"):
        cafe_items, total_cafe = naver_search("cafearticle", search_query, display, headers)
        for item in cafe_items:
            item["_source"] = "카페"
        all_items.extend(cafe_items)

    # 제외 키워드 파싱
    exclude_keywords = [k.strip() for k in exclude_raw.split(',') if k.strip()]

    # 날짜 파싱 및 필터링
    for item in all_items:
        item["_date"] = parse_item_date(item)

    if start_date or end_date:
        def in_range(item):
            d = item["_date"]
            if d is None:
                return False
            if start_date and d < start_date:
                return False
            if end_date and d > end_date:
                return False
            return True
        all_items = [i for i in all_items if in_range(i)]

    # 제외 키워드 필터링
    if exclude_keywords:
        def not_excluded(item):
            text = (strip_html(item.get("title", "")) + " " + strip_html(item.get("description", ""))).lower()
            return not any(kw.lower() in text for kw in exclude_keywords)
        all_items = [i for i in all_items if not_excluded(i)]

    # 날짜 내림차순 정렬
    all_items.sort(key=lambda x: x["_date"] or date.min, reverse=True)

    if not all_items:
        return jsonify({
            "results": [],
            "total_blog": total_blog,
            "total_cafe": total_cafe,
            "keyword": keyword,
        })

    # Claude에 보낼 텍스트 구성
    reviews_text = ""
    for i, item in enumerate(all_items, 1):
        title = strip_html(item.get("title", ""))
        description = strip_html(item.get("description", ""))
        reviews_text += f"[{i}] 제목: {title}\n설명: {description}\n\n"

    # Claude API로 정보 추출
    try:
        response = claude.messages.parse(
            model="claude-opus-4-6",
            max_tokens=2048,
            messages=[{
                "role": "user",
                "content": f"""다음은 해외 직구 관련 후기 목록입니다. 각 후기에서 정보를 추출해주세요.

추출 항목:
- brand_name: 브랜드명 (예: Nike, Apple, Zara, 뉴발란스 등, 없으면 null)
- product_name: 구체적인 상품명/모델명 (예: Air Max 90, iPhone 15 등, 없으면 null)
- category: 카테고리 (신발/의류/전자제품/가방/화장품/식품/기타 중 하나, 파악 불가시 null)
- purchase_source: 구매처 (아마존/이베이/알리익스프레스/직접구매/구매대행 등, 없으면 null)

후기 목록:
{reviews_text}
index는 후기 번호 [1], [2] 등 숫자를 그대로 사용하세요."""
            }],
            output_format=ExtractionResult,
        )
    except anthropic.APIError as e:
        return jsonify({"error": f"Claude API 오류: {str(e)}"}), 500

    extracted_map = {item.index: item for item in response.parsed_output.items}

    results = []
    for i, item in enumerate(all_items, 1):
        ext = extracted_map.get(i)
        src = item.get("_source", "블로그")
        results.append({
            "index": i,
            "source": src,
            "title": strip_html(item.get("title", "")),
            "description": strip_html(item.get("description", "")),
            "link": item.get("link", ""),
            "author": item.get("bloggerName") or item.get("cafename") or "",
            "postdate": format_date(item["_date"]),
            "brand_name": ext.brand_name if ext else None,
            "product_name": ext.product_name if ext else None,
            "category": ext.category if ext else None,
            "purchase_source": ext.purchase_source if ext else None,
        })

    return jsonify({
        "results": results,
        "total_blog": total_blog,
        "total_cafe": total_cafe,
        "keyword": keyword,
    })


def main():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 80)))


if __name__ == "__main__":
    main()
