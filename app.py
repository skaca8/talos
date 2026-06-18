# -*- coding: utf-8 -*-
"""
Talos 백엔드 (백엔드_명세.md 재현)
- GET /        : 루트 Talos_live.html 서빙 (§3.1 패치 멱등 적용, no-store, 매 요청 신선 로드)
- POST /report : 보고 → 통합 에이전트(Claude) 1회 호출 → 판정 JSON

계약 주의(중요): 현재 루트 Talos_live.html(언더스코어)의 applyResult는 응답을 *전부 평문 문자열*로
소비하고, kind 는 '시설'|'소모품' *평문 문자열* 이다(과거의 {ko,en} 객체/영문필드 계약은 무효).
=> COMBINED_SYS 가 평문 kind 를 내도록 하고, 모델이 객체/영문으로 줘도 평문화/한글화하는 방어코드를 둔다.
"""
import os
import re
import json

from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ---- 환경 ----
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Talos_live.html")
MODEL = "claude-haiku-4-5-20251001"           # 가장 빠른 등급 + 비전
API_KEY_ENV = "ANTHROPIC_API_KEY"

app = FastAPI(title="Talos backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ---- Anthropic 클라이언트 지연 초기화 (키 없어도 서버는 뜸) ----
_client = None
def get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=os.environ.get(API_KEY_ENV))
    return _client


# ============== §3.1 서빙-타임 HTML 패치 (멱등) ==============
# self-refetch(dc-runtime) HTML이면 자기 자신 재요청을 reject 로 바꿔 깨짐 방지.
# 루트 파일은 일반 단독 HTML이라 마커가 없어 no-op(원본 그대로). 멱등 보장.
_PATCH_FIND = 'fetch(location.href).then((res) => res.ok ? res.text() : "")'
_PATCH_REPL = ('Promise.reject(new Error("dc-selfrefetch-off"))'
               '.then((res) => res.ok ? res.text() : "")')

def patch_html(src: str) -> str:
    if _PATCH_FIND in src:
        src = src.replace(_PATCH_FIND, _PATCH_REPL)
    return src


@app.get("/")
def index():
    # 매 요청 신선 로드(캐시X) → HTML 수정 시 서버 재시작 불필요(수정내역 0-2 함정 해소)
    try:
        with open(HTML_PATH, "r", encoding="utf-8") as f:
            html = patch_html(f.read())
    except FileNotFoundError:
        return Response("Talos_live.html not found", status_code=500)
    return Response(content=html, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


# ============== §5 통합 에이전트 프롬프트 ==============
# 명세 §5 판정 로직 그대로. 단 출력 kind 는 현재 루트 프론트 계약대로 평문 한글('시설'|'소모품').
COMBINED_SYS = """너는 호텔 하우스키핑 보고를 한 번에 처리하는 AI다. 메이드의 '새 보고'(음성 받아쓴 텍스트, 사진 가능)와 그 객실에 '이미 접수된 문제들'을 보고 JSON 하나만 출력한다.
{
  "room": "<객실번호 숫자>",
  "room_status": "<dirty|cleaning|ready|hold>",
  "has_issue": true,
  "duplicate": false,
  "review": false,
  "issue_text": "<이번 보고의 문제 한 줄, 없으면 \\"\\">",
  "short": "<대시보드용 15자 이내 요약, 없으면 \\"\\">",
  "summary": "<이 객실의 열린 문제(기존+이번)를 모두 합친 1~2문장 요약, 없으면 \\"\\">",
  "urgency": "<urgent|normal>",
  "kind": "<시설|소모품>",
  "message": "<관리자/메이드용 한 문장>",
  "ai_read": "<사진이 있으면 하자 판독 1문장 + (위험도: 경미|보통|심각), 사진 없으면 \\"\\">"
}
판단(보고 텍스트와 사진을 반드시 함께 종합해 사용, 둘 중 하나만 보고 단정 금지, 지어내기 금지):
- 텍스트·사진 종합: 음성 텍스트와 사진을 교차 검증해 판단한다. 손상 '대상'과 '위치'는 보고 텍스트를 우선 기준으로 삼는다(예: 텍스트가 '창문'이면 사진을 '거울'이라 부르지 말 것). 텍스트에 없는 위치(예: '욕실')나 물건을 임의로 추가·추측하지 말 것. 사진은 그 대상의 손상 범위·정도·위험을 뒷받침하는 근거로만 쓴다. 텍스트가 없고 사진만 있으면 사진에서 확실히 보이는 것만 기술한다. issue_text/short/summary 의 대상·위치도 텍스트와 일치시킨다.
- 객실번호: 보고에 있으면 그 번호, 없으면 '현재 작업 객실'을 사용.
- review: 보고가 불명확하거나 객실 이슈와 무관(알아들을 수 없는 말, 의미 없는 잡담, 판단 불가)하면 true. 이때 has_issue=true, urgency="normal", issue_text/short/summary 모두 "확인 필요", room_status는 현재 상태 그대로(판매보류 금지), message="확인이 필요한 보고입니다". 명확한 실제 이슈면 review=false.
- duplicate: 이번 보고가 '이미 접수된 문제들' 중 하나와 사실상 같은 내용(같은 증상·같은 위치)이면 true. 새로운 문제면 false. 이미 접수된 문제가 없으면 false.
- summary: 이 객실의 열린 문제 전체를 종합한 요약. duplicate=true면 기존 문제들만 요약(새 항목 추가 금지). 여러 개면 모두 포함.
- 미해결 시설/비품 이슈가 있거나 사진상 판매 불가 하자가 있으면 → has_issue=true, room_status="hold".
- 이슈 없고 청소 완료면 → has_issue=false, room_status="ready", issue_text="", summary="".
- 청소 진행 중이면 → room_status="cleaning".
- 기존 이슈를 '수리/해결 완료'한 보고면 → has_issue=false, room_status="ready".
- kind: 설비·누수·파손·고장이면 "시설" / 수건·미니바·어메니티·소모품이면 "소모품". 반드시 "시설" 또는 "소모품" 둘 중 하나(한글 평문).
- urgency: 누수·파손·깨짐(유리·창문·거울 등 다칠 위험)·안전·위생·전기/화재 위험이 하나라도 있으면 urgent. 단순 비품 부족이나 경미한 점검만이면 normal. 또한 사진 판독(ai_read)의 위험도가 '심각'이면 반드시 urgency="urgent". 사진상 명백한 심각 안전 하자(깨진 유리·파편 등)는 불명확 보고가 아니므로 review로 분류하지 말고 실제 이슈(review=false)로 처리한다.
- short: 대시보드 칸용 15자 이내(예: "타일 균열·누수"). 여러 문제면 대표/최신 기준. has_issue=false면 "".
- ai_read: 사진이 있을 때만 작성. 보고 텍스트가 가리키는 대상(예: 창문)과 일치시켜, 사진 속 손상의 범위·정도·위험을 1문장으로 판독하고 끝에 (위험도: 경미|보통|심각)를 붙인다. 텍스트와 다른 대상/위치(예: 거울·욕실)를 지어내지 말 것. 사진 없으면 "".
JSON 외 텍스트 금지."""


# ============== 유틸 ==============
def digits(s) -> str:
    if s is None:
        return ""
    return re.sub(r"\D", "", str(s))

def parse_json(t: str) -> dict:
    if not t:
        return {}
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}

def _flat(v) -> str:
    """모델이 {ko,en} 객체/None 으로 줘도 평문 한글 문자열로 강제."""
    if v is None:
        return ""
    if isinstance(v, dict):
        return str(v.get("ko") or v.get("en") or "")
    return str(v)

def _kind_str(v) -> str:
    """kind 를 '시설'|'소모품' 평문 한글로 강제(객체/영문도 흡수)."""
    s = _flat(v).strip().lower()
    if any(k in s for k in ("소모품", "비품", "supply", "amenity", "towel", "미니바")):
        return "소모품"
    if any(k in s for k in ("시설", "facility", "leak", "broken")):
        return "시설"
    # 한글 원문 보존(이미 '시설'/'소모품'이면 그대로, 아니면 기본 시설)
    raw = _flat(v).strip()
    if raw in ("시설", "소모품"):
        return raw
    return "시설"

def call_agent(system: str, content) -> str:
    msg = get_client().messages.create(
        model=MODEL, max_tokens=600, system=system,
        messages=[{"role": "user", "content": content}],
    )
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


# ============== §6 /report 처리 (정확 순서) ==============
@app.post("/report")
async def report(request: Request):
    try:
        r = await request.json()
    except Exception:
        r = {}

    hint = digits(r.get("room_hint"))
    report_text = (r.get("text") or "").strip() or "(텍스트 없음, 사진만)"
    existing = r.get("existing") or []
    existing_str = json.dumps(existing, ensure_ascii=False) if existing else "없음"

    body = (
        f"현재 작업 객실(보고에 번호 없으면 이걸 사용): {hint or '미상'}\n"
        f"이 객실에 이미 접수된 문제들: {existing_str}\n"
        f"메이드 새 보고: {report_text}"
    )

    image_b64 = r.get("image_base64")
    if image_b64:
        media_type = r.get("image_media_type") or "image/jpeg"
        content = [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": media_type, "data": image_b64}},
            {"type": "text", "text": body},
        ]
    else:
        content = body

    try:
        out = parse_json(call_agent(COMBINED_SYS, content))
    except Exception as e:
        # 키 누락/크레딧 부족/네트워크 등 → 500으로 알림(프론트가 토스트로 표시)
        return JSONResponse({"error": str(e)}, status_code=500)

    # --- 방어/보정: 현재 루트 프론트 계약(평문 문자열, 평문 kind)으로 정규화 ---
    out["room"] = digits(out.get("room")) or hint
    out["room_status"] = (out.get("room_status") or "").strip() or "ready"
    out["has_issue"] = bool(out.get("has_issue"))
    out["duplicate"] = bool(out.get("duplicate"))
    out["review"] = bool(out.get("review"))
    out["issue_text"] = _flat(out.get("issue_text"))
    out["short"] = _flat(out.get("short"))
    out["summary"] = _flat(out.get("summary"))
    out["urgency"] = "urgent" if _flat(out.get("urgency")).strip() == "urgent" else "normal"
    out["kind"] = _kind_str(out.get("kind"))
    out["message"] = _flat(out.get("message"))
    out["ai_read"] = _flat(out.get("ai_read")) if image_b64 else ""
    # AI 판독 위험도 '심각' → 긴급 강제(이슈 리스트 일반/긴급 배지 연동)
    if "심각" in out["ai_read"]:
        out["urgency"] = "urgent"

    return JSONResponse(out)
