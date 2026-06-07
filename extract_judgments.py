#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
판결문 PDF → '범죄사실' 텍스트 블록 추출 파이프라인 (계획서 2단계)

흐름:
  1) PyMuPDF 로 텍스트 레이어 추출 (OCR 불필요, 외부 프로그램 불필요)
  2) 노이즈 제거 (페이지번호 / LBOX 출처표시 / 폼피드)
  3) '범죄사실' ~ '증거의 요지'('법령의 적용' 폴백) 구간만 슬라이스
  4) 줄바꿈 복원 (한국어 줄 중간 분절 → 자연스러운 문장)
  5) 개인정보 안전장치 마스킹 + 표기 정규화
       (i) 공백/줄바꿈 정리  (ii) 특수문자(가운뎃점·대시·인용부호) 통일
       (iii) 숫자·단위 표기 통일(금액 → 정수 원 단일형)
       (iv) 이형태 표준형 통일(예: 카톡 → 카카오톡)
  6) case_id ↔ 정제 텍스트 매핑을 JSONL 로 저장
"""

import re
import json
from pathlib import Path
import fitz   # PyMuPDF


# ──────────────────────────────────────────────────────────────
# 1) PDF → raw text  (PyMuPDF 사용 — 외부 프로그램/경로 설정 불필요)
# ──────────────────────────────────────────────────────────────
def pdf_to_text(pdf_path: Path) -> str:
    """PyMuPDF 로 전체 페이지 텍스트를 추출한다."""
    doc = fitz.open(str(pdf_path))
    parts = [page.get_text() for page in doc]
    doc.close()
    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────
# 2) 노이즈 제거
# ──────────────────────────────────────────────────────────────
RE_PAGENUM = re.compile(r"^-?\s*\d{1,4}\s*-?$")          # - 1 - , 1 , -12-
RE_LBOX    = re.compile(r"lbox\.kr/case")                 # LBOX 출처표시
RE_OUTSRC  = re.compile(r"^\s*출처\s*:")                  # '출처:' 시작 줄

def strip_noise(text: str) -> str:
    keep = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            keep.append("")            # 빈 줄은 문단 경계로 일단 유지
            continue
        if RE_PAGENUM.fullmatch(s):
            continue
        if RE_LBOX.search(s) or RE_OUTSRC.match(s):
            continue
        keep.append(line)
    return "\n".join(keep)


# ──────────────────────────────────────────────────────────────
# 3) 범죄사실 구간 슬라이스 (자간 공백 허용: '범 죄 사 실')
#    헤더 줄에 번호/괄호가 붙은 경우도 허용: '1. 범죄사실', '범죄사실(생략)'
# ──────────────────────────────────────────────────────────────
# '범죄사실' 헤더: 줄 시작에서 시작하되 뒤에 각주마커(<각주3>)/괄호/번호가 붙어도 허용.
# 단, 바로 뒤에 한글 조사(…범죄사실은/과/기재)가 오면 본문 언급이므로 헤더로 보지 않는다.
RE_START = re.compile(
    r"^[ \t]*(?:\d+\s*[.)]\s*)?범\s*죄\s*사\s*실"
    r"(?:\s*<[^>\n]*>)?(?:\s*\([^)\n]*\))?"
    r"(?=\s*(?:[\[\(<0-9]|$))",
    re.MULTILINE,
)
# 종료 헤더: 줄 단독뿐 아니라 '…송금받았다. 증거의 요지'처럼 줄 끝에 붙은 경우도 잡는다.
# (줄 시작 앵커 없이) 헤더 뒤가 줄끝/개행/번호/대괄호이면 헤더로 인정.
# 한글 조사(증거의 요지는/를 …)가 뒤따르면 본문 언급이므로 제외된다.
RE_END_1 = re.compile(
    r"증\s*거\s*의\s*요\s*지(?:\s*<[^>\n]*>)?\s*(?=$|\n|[\[\(0-9])",
    re.MULTILINE,
)
RE_END_2 = re.compile(
    r"법\s*령\s*의\s*적\s*용(?:\s*<[^>\n]*>)?\s*(?=$|\n|[\[\(0-9])",
    re.MULTILINE,
)

# ── 내부 소제목: 배경(제거) vs 범행(보존) ─────────────────────
# 대괄호/꺾쇠/번호형(1. 전제사실 등) 모두 인식. 자간 공백 허용.
_BG_NAMES = (
    r"기\s*초\s*사\s*실|전\s*제\s*사\s*실|공\s*모\s*관\s*계|"
    r"범\s*죄\s*전\s*력|범\s*죄\s*경\s*력"
)
_CRIME_SPECIFIC = r"구\s*체\s*적\s*범\s*죄\s*사\s*실"
_CRIME_GENERAL = r"범\s*죄\s*사\s*실"

RE_BRACKET_SECTION = re.compile(
    rf"[\[\【〈<]\s*({_BG_NAMES}|{_CRIME_SPECIFIC}|{_CRIME_GENERAL})\s*[\]\】〉>]",
    re.IGNORECASE,
)
RE_NUM_SECTION = re.compile(
    rf"(?:^|\n)\s*(?:\d+\s*[.)]\s*)?({_BG_NAMES}|{_CRIME_SPECIFIC}|{_CRIME_GENERAL})"
    rf"(?:\s*<[^>\n]*>)?(?=\s*(?:[\n\d\[\【〈<]|$))",
    re.MULTILINE | re.IGNORECASE,
)
RE_CASE_MARKER = re.compile(r"『[^』]+』|\[\d{4}[가-힣]+\d+[^\]]*\]")


def _norm_hdr(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _section_kind(label: str) -> str:
    """bg | crime_specific | crime_general | other"""
    n = _norm_hdr(label)
    if "구체적" in n and "범죄사실" in n:
        return "crime_specific"
    if n == "범죄사실":
        return "crime_general"
    for bg in ("기초사실", "전제사실", "공모관계", "범죄전력", "범죄경력"):
        if n == bg:
            return "bg"
    return "other"


def _find_sections(text: str):
    """(start, end, kind) 목록 — end는 다음 섹션 시작 또는 문서 끝."""
    hits = []
    for m in RE_BRACKET_SECTION.finditer(text):
        hits.append((m.start(), m.end(), _section_kind(m.group(1)), m.group(1)))
    for m in RE_NUM_SECTION.finditer(text):
        # 괄호형과 겹치면 스킵
        if any(abs(m.start() - b[0]) < 3 for b in hits):
            continue
        hits.append((m.start(), m.end(), _section_kind(m.group(1)), m.group(1)))
    if not hits:
        return []
    hits.sort(key=lambda x: x[0])
    out = []
    for i, (st, en, kind, _lbl) in enumerate(hits):
        nxt = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        out.append((en, nxt, kind))
    return out


def _crime_text_from_sections(text: str) -> str:
    secs = _find_sections(text)
    if not secs:
        return ""
    parts = []
    for st, en, kind in secs:
        if kind == "crime_specific":
            parts.append(text[st:en].strip())
    if parts:
        return "\n".join(parts)
    for st, en, kind in secs:
        if kind == "crime_general":
            parts.append(text[st:en].strip())
    return "\n".join(parts).strip()


def _split_merged_chunks(text: str):
    """병합 판결: 사건번호 경계로 분할 후 각각 범행만 추출."""
    markers = list(RE_CASE_MARKER.finditer(text))
    if not markers:
        return [_crime_text_from_sections(text) or text.strip()]

    chunks = []
    pos = 0
    for m in markers:
        if m.start() > pos:
            seg = text[pos:m.start()]
            crime = _crime_text_from_sections(seg)
            if crime:
                chunks.append(crime)
        pos = m.end()
    if pos < len(text):
        seg = text[pos:]
        crime = _crime_text_from_sections(seg)
        if crime:
            chunks.append(crime)
    return [c for c in chunks if c]


def _drop_basis_block(block: str) -> str:
    """
    배경 소제목 제거 후 범행 서술만 반환.
    보존: [구체적 범죄사실], [범죄사실](내부 번호형 전제사실·공모관계 등 제외)
    제거: 기초사실, 전제사실, 공모관계, 범죄전력, 범죄경력
    """
    block = block.strip()
    if not block:
        return ""

    merged = _split_merged_chunks(block)
    if len(merged) > 1:
        joined = "\n\n".join(merged).strip()
        if joined:
            return joined

    crime = _crime_text_from_sections(block)
    if crime:
        return crime

    # 폴백: [범죄사실] 괄호 이후 (구형)
    m_crime = re.search(
        rf"[\[\【]\s*{_CRIME_GENERAL}\s*[\]\】]",
        block,
        re.IGNORECASE,
    )
    if m_crime:
        tail = block[m_crime.end():].strip()
        inner = _crime_text_from_sections(tail)
        return inner or tail

    # 폴백: 첫 피고인/성명불상 서술 이후 (배경 단락 스킵)
    m_sig = re.search(r"(피고인은|피고인\s*[A-Z가-힣]?\s*은|성명불상)", block)
    if m_sig:
        return block[m_sig.start():].strip()
    return block.strip()


def slice_crime_facts(text: str):
    m_start = RE_START.search(text)
    if not m_start:
        return None, "NO_START_HEADER"
    start = m_start.end()

    m_end = RE_END_1.search(text, start) or RE_END_2.search(text, start)
    if not m_end:
        raw_block = text[start:].strip()
        status = "NO_END_HEADER(use_EOF)"
    else:
        raw_block = text[start:m_end.start()].strip()
        status = "OK"

    cleaned = _drop_basis_block(raw_block)
    if not cleaned:
        cleaned = raw_block
    return cleaned, status


# ──────────────────────────────────────────────────────────────
# 4) 줄바꿈 복원
#    - 직전 줄이 문장종결/부호로 끝나면 공백으로 연결
#    - 아니면(한글·숫자 중간 분절) 직접 붙임  → "내\n용으로" → "내용으로"
# ──────────────────────────────────────────────────────────────
SENT_END = tuple(".。!?\"'」』）)]:;,")

def reflow(text: str) -> str:
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]      # 빈 줄 제거
    if not lines:
        return ""
    buf = lines[0]
    for ln in lines[1:]:
        if buf and buf[-1] in SENT_END:
            buf += " " + ln
        else:
            buf += ln
    return buf


# ──────────────────────────────────────────────────────────────
# 5) 개인정보 안전장치 마스킹 + 표기 정규화
#    (금액·날짜는 수법 분석에 필요하므로 보존)
# ──────────────────────────────────────────────────────────────
RE_PHONE = re.compile(r"\b0\d{1,2}[- ]?\d{3,4}[- ]?\d{4}\b")
RE_RRN   = re.compile(r"\b\d{6}[- ]?\d{7}\b")            # 주민등록번호
RE_ACCT  = re.compile(r"\b\d{2,6}-\d{2,6}-\d{2,7}\b")    # 계좌번호 형태

def mask_pii(text: str) -> str:
    text = RE_RRN.sub("[주민번호]", text)
    text = RE_PHONE.sub("[전화번호]", text)
    text = RE_ACCT.sub("[계좌번호]", text)
    return text


# ── (ii) 특수문자 정규화 테이블 ───────────────────────────────
MIDDOT_CHARS = "\u00b7\u0387\u2022\u2219\u22c5\u30fb\uff65\u2024"  # 각종 가운뎃점/불릿
DASH_CHARS   = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d"  # 각종 대시/하이픈
TILDE_CHARS  = "\u223c\uff5e\u301c"                                # 물결 변형

_TRANS = {ord(c): "\u00b7" for c in MIDDOT_CHARS}
_TRANS.update({ord(c): "-" for c in DASH_CHARS})
_TRANS.update({ord(c): "~" for c in TILDE_CHARS})
_TRANS.update({
    ord("\u201c"): '"', ord("\u201d"): '"',   # “ ”
    ord("\u2018"): "'", ord("\u2019"): "'",   # ‘ ’
})

# ── (iii) 금액(숫자·단위) → 정수 '원' 단일형 ──────────────────
#   '100만 원' / '100만원' / '1,000,000원' → 모두 '1000000원'
RE_MONEY = re.compile(r"\d[\d,]*\s*(?:(?:억|만|천|백|십)\s*\d*[\d,]*\s*)*원")
_UNIT_SECTION = {"억": 10 ** 8, "만": 10 ** 4}
_UNIT_SMALL   = {"천": 1000, "백": 100, "십": 10}

def _parse_won(expr: str):
    """'1억 2,000만원' 같은 표현을 정수(원)로 환산. 실패 시 None."""
    s = expr.replace("원", "").replace(",", "").replace(" ", "")
    total, section, digits = 0, 0, ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        elif ch in _UNIT_SMALL:
            n = int(digits) if digits else 1
            section += n * _UNIT_SMALL[ch]
            digits = ""
        elif ch in _UNIT_SECTION:
            n = int(digits) if digits else 0
            section += n
            total += section * _UNIT_SECTION[ch]
            section, digits = 0, ""
        else:
            return None
    if digits:
        section += int(digits)
    return total + section

def _money_repl(m: re.Match) -> str:
    val = _parse_won(m.group())
    return f"{val}원" if val is not None else m.group()

# ── (iv) 자주 등장하는 이형태 → 표준형 통일 ───────────────────
#   약어가 표준형의 일부인 경우(인스타→인스타그램 등)는 lookahead 로 중복확장 방지.
VARIANT_RULES = [
    (re.compile(r"카톡"),            "카카오톡"),
    (re.compile(r"페북"),            "페이스북"),
    (re.compile(r"인스타(?!그램)"),  "인스타그램"),
    (re.compile(r"텔레(?!그램)그렘"),"텔레그램"),
    (re.compile(r"핸드폰|핸폰|휴대폰"), "휴대전화"),
    (re.compile(r"비번"),            "비밀번호"),
]

def normalize(text: str) -> str:
    text = text.translate(_TRANS)                 # (ii) 인용부호/가운뎃점/대시/물결 통일
    text = RE_MONEY.sub(_money_repl, text)        # (iii) 금액 단일형
    for pat, rep in VARIANT_RULES:                # (iv) 이형태 표준형
        text = pat.sub(rep, text)
    text = re.sub(r"[ \t]+", " ", text)           # (i) 다중 공백 정리
    return text.strip()


# ──────────────────────────────────────────────────────────────
# 6) 사건 메타데이터 추출
# ──────────────────────────────────────────────────────────────
RE_CASENO = re.compile(r"(\d{4}\s*[가-힣]{1,3}\s*\d+)")   # 2024고단657, 2025고합80
# 법원명: 대법원 / ○○고등법원 / ○○지방법원(+ ○○지원)
RE_COURT  = re.compile(r"(대법원|[가-힣]+고등법원|[가-힣]+지방법원(?:\s*[가-힣]+지원)?)")

def extract_meta(raw_text: str, fallback_name: str):
    head = raw_text[:600]
    m_case = RE_CASENO.search(head)
    case_no = re.sub(r"\s+", "", m_case.group(1)) if m_case else fallback_name

    # 1순위: 본문 머리에서 법원명 탐색 (자간 공백 제거 후)
    head_nospace = re.sub(r"\s+", "", head)
    m_court = RE_COURT.search(head_nospace)
    if m_court:
        court = m_court.group(1)
    else:
        # 2순위: 파일명에서 폴백
        m2 = RE_COURT.search(re.sub(r"\s+", "", fallback_name))
        court = m2.group(1) if m2 else ""
    return case_no, court


# ──────────────────────────────────────────────────────────────
# 메인 파이프라인
# ──────────────────────────────────────────────────────────────
# 폴더명 → 피싱 유형 라벨 매핑 (폴더명에 아래 키워드가 들어 있으면 해당 유형)
TYPE_KEYWORDS = {
    "로맨스": "로맨스스캠",
    "메신저": "메신저피싱",
    "보이스": "보이스피싱",
    "스미싱": "스미싱",
    "투자":   "투자리딩방",
    "팀미션": "팀미션",
}

def folder_to_type(folder_name: str) -> str:
    for key, label in TYPE_KEYWORDS.items():
        if key in folder_name:
            return label
    return "미분류"


def process_one(pdf_path: Path, phishing_type: str) -> dict:
    raw = pdf_to_text(pdf_path)
    case_no, court = extract_meta(raw, pdf_path.stem)
    denoised = strip_noise(raw)
    block, status = slice_crime_facts(denoised)

    if block is None:
        return {
            "case_id": case_no, "phishing_type": phishing_type, "court": court,
            "source_file": pdf_path.name, "status": status,
            "char_len": 0, "text": "",
        }

    text = normalize(mask_pii(reflow(block)))
    return {
        "case_id": case_no, "phishing_type": phishing_type, "court": court,
        "source_file": pdf_path.name, "status": status,
        "char_len": len(text), "text": text,
    }


def main(root_dir: str, output_jsonl: str):
    root = Path(root_dir)
    # 루트 바로 아래의 PDF + 모든 하위 폴더의 PDF 를 전부 수집
    pdfs = sorted(root.rglob("*.pdf"))
    if not pdfs:
        print(f"[오류] '{root}' 아래에서 PDF 를 찾지 못했습니다. 경로를 확인하세요.")
        return []

    records, fails = [], []
    type_counter = {}
    for pdf in pdfs:
        # PDF 가 들어 있는 '폴더명' 으로 유형 판정
        ptype = folder_to_type(pdf.parent.name)
        type_counter[ptype] = type_counter.get(ptype, 0) + 1
        rec = process_one(pdf, ptype)
        records.append(rec)
        if rec["status"] != "OK" or rec["char_len"] < 50:
            fails.append((pdf.parent.name, pdf.name, rec["status"], rec["char_len"]))

    with open(output_jsonl, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"총 {len(records)}건 처리 → {output_jsonl}")
    print(f"정상(OK) {sum(1 for r in records if r['status']=='OK')}건 / "
          f"점검필요 {len(fails)}건\n")

    print("[유형별 건수]")
    for t, n in sorted(type_counter.items()):
        print(f"  - {t}: {n}건")

    if fails:
        print("\n[점검 필요 목록]")
        for folder, name, st, n in fails:
            print(f"  - [{folder}] {name} | {st} | {n}자")
    return records


if __name__ == "__main__":
    import sys
    # 사용법: python extract_judgments.py "엘박스 판결문 모음 폴더" "judgments.jsonl"
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    out  = sys.argv[2] if len(sys.argv) > 2 else "judgments_extracted.jsonl"
    main(root, out)
