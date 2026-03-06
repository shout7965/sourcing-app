import os
import re
import requests
import anthropic
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


def format_date(date_str: str) -> str:
    """YYYYMMDD → YYYY-MM-DD"""
    if len(date_str) == 8:
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    return date_str


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

    if not keyword:
        return jsonify({"error": "키워드를 입력해주세요."}), 400

    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return jsonify({"error": "네이버 API 키가 설정되지 않았습니다. .env 파일을 확인해주세요."}), 500

    # 네이버 블로그 검색
    search_query = f"{keyword} 직구 후기"
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {
        "query": search_query,
        "display": display,
        "sort": "sim",
    }

    try:
        naver_resp = requests.get(
            "https://openapi.naver.com/v1/search/blog.json",
            headers=headers,
            params=params,
            timeout=10,
        )
    except requests.RequestException as e:
        return jsonify({"error": f"네이버 API 연결 실패: {str(e)}"}), 500

    if naver_resp.status_code != 200:
        return jsonify({"error": f"네이버 API 오류 ({naver_resp.status_code}): {naver_resp.text}"}), 500

    naver_data = naver_resp.json()
    items = naver_data.get("items", [])

    if not items:
        return jsonify({"results": [], "total": 0, "keyword": keyword})

    # Claude에 보낼 텍스트 구성
    reviews_text = ""
    for i, item in enumerate(items, 1):
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
                "content": f"""다음은 해외 직구 관련 블로그 후기 목록입니다. 각 후기에서 정보를 추출해주세요.

추출 항목:
- brand_name: 브랜드명 (예: Nike, Apple, Zara, 뉴발란스 등, 없으면 null)
- product_name: 구체적인 상품명/모델명 (예: Air Max 90, iPhone 15, M34 등, 없으면 null)
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
    for i, item in enumerate(items, 1):
        ext = extracted_map.get(i)
        results.append({
            "index": i,
            "title": strip_html(item.get("title", "")),
            "description": strip_html(item.get("description", "")),
            "link": item.get("link", ""),
            "blogger_name": item.get("bloggerName", ""),
            "postdate": format_date(item.get("postdate", "")),
            "brand_name": ext.brand_name if ext else None,
            "product_name": ext.product_name if ext else None,
            "category": ext.category if ext else None,
            "purchase_source": ext.purchase_source if ext else None,
        })

    return jsonify({
        "results": results,
        "total": naver_data.get("total", 0),
        "keyword": keyword,
    })


def main():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 80)))


if __name__ == "__main__":
    main()
