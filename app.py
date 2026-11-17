"""사외협력사 기초안전교육 시험 — Streamlit 버전.

- 문항: questions.json (「현대제철 Safety Guide(수급업체용, 2026년)」 기준 30문항)
- 응시 결과·진행 상황은 응시자 휴대폰의 브라우저 저장소(localStorage)에만 저장한다.
  서버에는 아무것도 저장하지 않는다 (별도 이력관리 없음).
- 실행: streamlit run app.py
- 테스트·로컬 확인용으로 브라우저 저장 없이 실행하려면 환경변수 SAFETY_QUIZ_STORAGE=memory
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

import logic as L

APP_DIR = Path(__file__).resolve().parent
RESULTS_KEY = "safetyQuiz.results.v1"
SESSION_KEY = "safetyQuiz.session.v1"
STORAGE_MODE = os.environ.get("SAFETY_QUIZ_STORAGE", "browser")
KST = timezone(timedelta(hours=9))
MARKS = ["①", "②", "③", "④", "⑤"]
ICONS = {"산업안전 일반": "🦺", "안전기준": "📋", "보건기준": "🩺",
         "공정안전관리": "🏭", "사고대응절차": "🚨", "유해화학물질관리": "🧪"}
DEFAULT_BASIS = "현대제철 세이프티 가이드(2026)"


@st.cache_data
def load_bank():
    data = json.loads((APP_DIR / "questions.json").read_text(encoding="utf-8"))
    return data["categories"], data["questions"], data.get("source", "")


CATEGORIES, QUESTIONS, SOURCE = load_bank()
Q_BY_ID = {q["id"]: q for q in QUESTIONS}
CAT_OF = {q["id"]: q["category"] for q in QUESTIONS}
ANSWER_OF = {q["id"]: q["answer"] for q in QUESTIONS}
ss = st.session_state


# ---------- 표시용 도우미 ----------
_MD_SPECIAL = re.compile(r"([\\`*_\[\]{}()#+\-.!|~$<>])")


def md(text: object) -> str:
    """사용자 입력·문항 문구를 마크다운으로 해석되지 않게 이스케이프."""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def question_md(text: str) -> str:
    """문항의 <strong>…</strong>만 굵게, 나머지는 그대로 표시."""
    out = []
    for part in re.split(r"(<strong>.*?</strong>)", text):
        m = re.fullmatch(r"<strong>(.*?)</strong>", part)
        out.append(f"**{md(m.group(1))}**" if m else md(part))
    return "".join(out)


def source_label(q: dict) -> str:
    return f'근거: {q.get("basis") or DEFAULT_BASIS} p.{q["source_page"]}'


def now_iso() -> str:
    return datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


# ---------- 브라우저 저장소 (응시자 휴대폰) ----------
def _js(expr: str, key: str):
    from streamlit_js_eval import streamlit_js_eval
    return streamlit_js_eval(js_expressions=expr, key=key)


_LOAD_JS = (
    "(() => { try { return JSON.stringify({r: localStorage.getItem(%s), s: localStorage.getItem(%s)}); }"
    " catch (e) { return 'BLOCKED'; } })()" % (json.dumps(RESULTS_KEY), json.dumps(SESSION_KEY))
)


def init_storage() -> bool:
    """저장된 결과와 진행 상황을 한 번 불러온다. 아직 브라우저 응답이 없으면 False."""
    if ss.get("loaded"):
        return True
    ss.setdefault("save_seq", 0)
    ss.setdefault("saved_seq", 0)
    ss.setdefault("store_ok", True)
    ss.setdefault("corrupt_raw", None)
    ss.setdefault("results", [])
    if STORAGE_MODE != "browser":
        ss.loaded = True
        return True
    raw = _js(_LOAD_JS, key="ls_load")
    if raw is None:
        return False
    stored_session = None
    if raw == "BLOCKED":
        ss.store_ok = False
    else:
        data = json.loads(raw)
        if data.get("r"):
            try:
                parsed = json.loads(data["r"])
                if not isinstance(parsed, list):
                    raise ValueError("not a list")
                ss.results = [r for r in parsed if L.is_valid_record(r)]  # 형식이 틀린 항목만 제외
            except (ValueError, TypeError):
                ss.corrupt_raw = data["r"]  # 손상된 데이터는 백업 후 빈 목록에서 다시 시작
                ss.results = []
                request_save()
        if data.get("s"):
            try:
                stored_session = json.loads(data["s"])
            except (ValueError, TypeError):
                stored_session = None
    ss.loaded = True
    restore_session(stored_session)
    return True


def request_save() -> None:
    ss.save_seq = ss.get("save_seq", 0) + 1


def flush_save() -> None:
    """저장 요청이 있으면 브라우저 저장소에 기록한다 (확인 응답을 받을 때까지 매 실행 시도)."""
    if STORAGE_MODE != "browser" or not ss.get("store_ok") or ss.saved_seq >= ss.save_seq:
        return
    seq = ss.save_seq
    results = json.dumps(json.dumps(ss.results, ensure_ascii=False))
    snap = json.dumps(json.dumps(session_snapshot(), ensure_ascii=False))
    backup = ""
    if ss.get("corrupt_raw"):
        backup = "localStorage.setItem(%s, %s);" % (json.dumps(RESULTS_KEY + ".corrupt"), json.dumps(ss.corrupt_raw))
    js = ("(() => { try { %s localStorage.setItem(%s, %s); localStorage.setItem(%s, %s); return %d; }"
          " catch (e) { return -1; } })()" % (backup, json.dumps(RESULTS_KEY), results, json.dumps(SESSION_KEY), snap, seq))
    res = _js(js, key=f"ls_save_{seq}")
    if res == seq:
        ss.saved_seq = seq
        ss.corrupt_raw = None
    elif res == -1:
        ss.store_ok = False


# ---------- 진행 상황 (새로고침·재접속 시 복원) ----------
def elapsed_sec() -> float:
    resumed = ss.get("resumed_at")
    return ss.get("elapsed_before", 0.0) + (time.time() - resumed if resumed else 0.0)


def quiz_active() -> bool:
    return bool(ss.get("quiz_items")) and not ss.get("finished", True)


def session_snapshot() -> dict:
    quiz = None
    if quiz_active():
        quiz = {"category": ss.category, "ids": [it["id"] for it in ss.quiz_items],
                "orders": [it["order"] for it in ss.quiz_items], "index": ss.index,
                "answers": ss.answers, "elapsed": elapsed_sec()}
    return {"user": ss.get("user"), "quiz": quiz}


def restore_session(data) -> None:
    ss.screen = "start"
    if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
        return
    u = {k: str(data["user"].get(k) or "") for k in ("company", "team", "name")}
    if not all(u.values()):
        # 소속 입력이 생기기 전 정보 등: 입력칸만 채워 두고 시작 화면에서 받는다
        ss.in_company, ss.in_team, ss.in_name = u["company"], u["team"], u["name"]
        return
    ss.user = u
    ss.screen = "category"
    quiz = data.get("quiz")
    if not isinstance(quiz, dict) or not quiz.get("ids"):
        return
    items = []
    for i, qid in enumerate(quiz["ids"]):
        q = Q_BY_ID.get(qid)
        orders = quiz.get("orders") or []
        order = orders[i] if i < len(orders) else None
        if not q or not isinstance(order, list) or sorted(order) != list(range(len(q["options"]))):
            return  # 문항이 바뀌었거나 데이터가 맞지 않으면 진행 상황은 버림
        items.append({"id": qid, "order": order})
    answers = quiz.get("answers")
    if not isinstance(answers, list) or len(answers) != len(items):
        answers = [None] * len(items)
    answers = [a if isinstance(a, int) and 0 <= a < len(Q_BY_ID[it["id"]]["options"]) else None
               for a, it in zip(answers, items)]
    index = quiz.get("index") if isinstance(quiz.get("index"), int) else 0
    ss.category = quiz.get("category")
    ss.quiz_items, ss.answers = items, answers
    ss.index = min(max(index, 0), len(items) - 1)
    ss.elapsed_before = max(0.0, float(quiz.get("elapsed") or 0))
    ss.resumed_at = time.time()
    ss.finished = False
    ss.quiz_token = random.randint(0, 10**9)
    ss.screen = "quiz"


# ---------- 동작 ----------
def go(screen: str) -> None:
    ss.screen = screen


def my_records() -> list[dict]:
    return [r for r in ss.results if L.same_person(r, ss.user)]


def start_quiz(category: str) -> None:
    pool = QUESTIONS if category == L.ALL else [q for q in QUESTIONS if q["category"] == category]
    pool = random.sample(pool, len(pool))  # 문항 순서 무작위
    ss.category = category
    ss.quiz_items = [{"id": q["id"], "order": random.sample(range(len(q["options"])), len(q["options"]))} for q in pool]
    ss.index = 0
    ss.answers = [None] * len(ss.quiz_items)
    ss.elapsed_before, ss.resumed_at = 0.0, time.time()
    ss.finished = False
    ss.quiz_token = random.randint(0, 10**9)
    ss.screen = "quiz"
    request_save()


def select_answer(orig: int) -> None:
    if ss.answers[ss.index] is None:  # 답을 고르면 확정 (재선택 불가)
        ss.answers[ss.index] = orig
        request_save()


def next_question() -> None:
    if ss.answers[ss.index] is None:
        return
    if ss.index < len(ss.quiz_items) - 1:
        ss.index += 1
        request_save()
    else:
        finish_quiz()


def finish_quiz() -> None:
    if ss.get("finished"):
        return  # 연타로 인한 중복 저장 방지
    ss.finished = True
    ids = [it["id"] for it in ss.quiz_items]
    record = L.make_record(ss.user, ss.category, ids, ss.answers, ANSWER_OF, round(elapsed_sec()), now_iso())
    ss.results = ss.results + [record]
    ss.last_record = record
    ss.screen = "result"
    request_save()


def change_user() -> None:
    ss.user = None
    ss.quiz_items, ss.finished = [], True
    ss.in_company = ss.in_team = ss.in_name = ""
    ss.screen = "start"
    request_save()


def open_ranking(tab: str | None, back: str) -> None:
    ss.ranking_tab = tab if tab in CATEGORIES + [L.ALL] else CATEGORIES[0]
    ss.ranking_back = back
    ss.screen = "ranking"


# ---------- 화면 ----------
def show_overall(o: dict) -> None:
    n = len(CATEGORIES)
    box = {"pass": st.success, "fail": st.error, "progress": st.info}[o["status"]]
    box(f"**{L.overall_title(o, n)}**  \n{md(L.overall_detail(o, n))}")


def screen_start() -> None:
    st.caption("⛑ 사외협력사 기초안전교육 · 현대제철 세이프티 가이드 기반")
    st.header("기초안전교육 시험")
    st.markdown(f"**{len(CATEGORIES)}개 카테고리 · {len(QUESTIONS)}문항 · 합격 {L.PASS_SCORE}점** "
                f"(6개 카테고리 모두 {L.PASS_SCORE}점 이상이면 종합 합격)")
    st.write("카테고리를 골라 문제를 풀고, 바로 정답과 해설을 확인하세요.")
    st.caption(f"모든 문항은 「{SOURCE}」 기준이며, 법령과 다른 경우 해설에 함께 안내합니다.")
    with st.form("start_form"):
        company = st.text_input("업체명", max_chars=40, placeholder="예: ○○건설", key="in_company")
        team = st.text_input("소속 (팀·부서)", max_chars=30, placeholder="예: 전기팀, 정비 1반", key="in_team")
        name = st.text_input("성명", max_chars=20, placeholder="예: 홍길동", key="in_name")
        st.caption("같은 업체의 동명이인은 소속으로 구분합니다. "
                   "수집 항목: 업체명, 소속, 성명 · 이용 목적: 응시 결과·순위 기록 · 이 휴대폰에만 저장됩니다")
        submitted = st.form_submit_button("시작하기", type="primary", width="stretch", key="btn_start")
    if submitted:
        user = {"company": company.strip(), "team": team.strip(), "name": name.strip()}
        if not all(user.values()):
            st.error("업체명, 소속, 성명을 모두 입력해 주세요.")
        else:
            ss.user = user
            ss.screen = "category"
            request_save()
            st.rerun()


def screen_category() -> None:
    st.markdown(f"**{md(L.person_label(ss.user))}**")
    st.subheader("카테고리를 선택하세요")
    mine = my_records()
    o = L.overall_of(mine, CATEGORIES, CAT_OF)
    show_overall(o)
    for cat in CATEGORIES:
        best = o["best"][cat]
        status = ("미응시" if best is None else
                  f"✔ 합격 · 최고 {best}점" if L.is_pass(best) else f"↻ 재응시 필요 · 최고 {best}점")
        n = sum(1 for q in QUESTIONS if q["category"] == cat)
        st.button(f"{ICONS.get(cat, '📘')} {cat} ({n}문항) · {status}", key=f"cat_{cat}",
                  on_click=start_quiz, args=(cat,), width="stretch")
    runs = [r["score"] for r in mine if r["category"] == L.ALL]
    label = f"전체 응시 ({len(QUESTIONS)}문항)"
    if runs:
        label += f" · 최고 {max(runs)}점" + (" 합격" if L.is_pass(max(runs)) else "")
    st.button(label, key="btn_all", type="primary", on_click=start_quiz, args=(L.ALL,), width="stretch")
    st.button("순위 보기", key="btn_to_ranking", on_click=open_ranking, args=(None, "category"), width="stretch")
    st.button("응시자 변경", key="btn_change_user", type="tertiary", on_click=change_user)


def screen_quiz() -> None:
    item = ss.quiz_items[ss.index]
    q = Q_BY_ID[item["id"]]
    n = len(ss.quiz_items)
    chosen = ss.answers[ss.index]
    answered = chosen is not None
    cat_label = f"전체 · {q['category']}" if ss.category == L.ALL else q["category"]
    st.caption(f"{cat_label} · {ss.index + 1} / {n}")
    st.progress((ss.index + (1 if answered else 0)) / n)
    st.markdown("### " + question_md(q["question"]), anchors=False)
    tok = ss.quiz_token
    if not answered:
        for pos, orig in enumerate(item["order"]):
            st.button(f"{MARKS[pos]} {md(q['options'][orig])}", key=f"opt_{tok}_{ss.index}_{pos}",
                      on_click=select_answer, args=(orig,), width="stretch")
        return
    for pos, orig in enumerate(item["order"]):
        text = f"{MARKS[pos]} {md(q['options'][orig])}"
        if orig == q["answer"]:
            st.markdown(f":green[**✔ {text}** — 정답]")
        elif orig == chosen:
            st.markdown(f":red[**✖ {text}** — 선택]")
        else:
            st.markdown(f":gray[{text}]")
    correct = chosen == q["answer"]
    body = f"**{'✔ 정답입니다' if correct else '✖ 오답입니다'}**"
    if not correct:
        pos = item["order"].index(q["answer"])
        body += f"  \n정답: {MARKS[pos]} {md(q['options'][q['answer']])}"
    body += f"  \n\n{md(q['explanation'])}"
    (st.success if correct else st.error)(body)
    st.caption(source_label(q))
    last = ss.index == n - 1
    st.button("결과 보기" if last else "다음 문제", key=f"next_{tok}_{ss.index}", type="primary",
              on_click=next_question, width="stretch")


def screen_result() -> None:
    r = ss.last_record
    st.caption("전체 응시" if r["category"] == L.ALL else r["category"])
    st.markdown(f"# {r['score']}점", anchors=False)
    if L.is_pass(r["score"]):
        st.success(f"**✔ 합격** (기준 {L.PASS_SCORE}점)")
    else:
        st.error(f"**✖ 불합격** (기준 {L.PASS_SCORE}점)")
    st.caption(f"정답 {r['correct']} / {r['total']}문항 · 소요 {r['duration_sec']}초")
    show_overall(L.overall_of(my_records(), CATEGORIES, CAT_OF))  # 이번 결과까지 반영
    if not ss.get("store_ok", True):
        st.warning("이 기기에 결과를 저장하지 못했습니다. 화면을 닫으면 기록이 사라질 수 있습니다.")
    wrong = r["wrong_ids"]
    st.subheader(f"틀린 문항 {len(wrong)}개" if wrong else "틀린 문항이 없습니다")
    for qid in wrong:
        q = Q_BY_ID.get(qid)
        if not q:
            continue
        with st.expander(md(re.sub(r"</?strong>", "", q["question"]))):
            st.markdown(f":green[**✔ 정답: {md(q['options'][q['answer']])}**]")
            st.write(q["explanation"])
            st.caption(source_label(q))
    st.button("다시 풀기", key="btn_retry", type="primary", on_click=start_quiz, args=(r["category"],), width="stretch")
    st.button("다른 카테고리 선택", key="btn_to_category", on_click=go, args=("category",), width="stretch")
    st.button("순위 보기", key="btn_result_ranking", on_click=open_ranking, args=(r["category"], "result"),
              width="stretch")


@st.dialog("응시 기록 지우기")
def confirm_clear() -> None:
    st.write("이 기기에 저장된 모든 응시 기록과 순위를 지울까요? 지운 기록은 되살릴 수 없습니다.")
    c1, c2 = st.columns(2)
    if c1.button("지우기", type="primary", key="btn_clear_ok", width="stretch"):
        ss.results = []
        request_save()
        st.rerun()
    if c2.button("취소", key="btn_clear_cancel", width="stretch"):
        st.rerun()


def screen_ranking() -> None:
    st.subheader("순위")
    tabs = CATEGORIES + [L.ALL]
    labels = [("전체 응시" if t == L.ALL else t) for t in tabs]
    default = labels[tabs.index(ss.get("ranking_tab", CATEGORIES[0]))]
    for t, tab in zip(tabs, st.tabs(labels, default=default)):
        with tab:
            st.caption(("전체 응시(30문항) 결과 기준" if t == L.ALL else t) + " · 1인 최고 기록")
            ranking = L.build_ranking(ss.results, t)
            if not ranking:
                st.info("아직 기록이 없습니다.")
                continue
            me = next((i for i, r in enumerate(ranking) if L.same_person(r, ss.user)), -1)
            lines = []
            for i, r in enumerate(ranking[:L.RANK_TOP]):
                medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i + 1}위"
                badge = ":green-badge[합격]" if L.is_pass(r["score"]) else ":red-badge[불합격]"
                who = md(L.person_label(r))
                row = f"{medal} {'**' + who + ' (나)**' if i == me else who} {badge} {r['score']}점 · {r['duration_sec']}초"
                lines.append(row)
            st.markdown("  \n".join(lines))
            if me >= L.RANK_TOP:
                st.markdown(f"**내 순위: {me + 1}위 / {len(ranking)}명 ({ranking[me]['score']}점)**")
    st.button("돌아가기", key="btn_ranking_back", on_click=go, args=(ss.get("ranking_back", "category"),),
              width="stretch")
    if ss.results and st.button("이 기기의 응시 기록 지우기", key="btn_clear_records", type="tertiary"):
        confirm_clear()


def screen_admin() -> None:
    st.subheader("관리자 · 응시 결과")
    st.caption("이 기기에 저장된 응시 결과만 표시됩니다.")
    rs = sorted(ss.results, key=lambda r: str(r.get("submitted_at", "")), reverse=True)
    people: dict[str, list] = {}
    for r in rs:
        people.setdefault(L.record_key(r), []).append(r)
    overall_pass = sum(1 for v in people.values() if L.overall_of(v, CATEGORIES, CAT_OF)["status"] == "pass")
    st.write(f"응시 {len(rs)}건 · 응시자 {len(people)}명 · 종합 합격 {overall_pass}명")
    st.download_button("응시 결과 CSV 다운로드", data=L.results_csv(rs), mime="text/csv",
                       file_name=f"기초안전교육_응시결과_{now_iso()[:10]}.csv", width="stretch", key="btn_csv")
    st.markdown("**응시 결과 목록**")
    st.dataframe([{"일시": str(r.get("submitted_at", ""))[:16].replace("T", " "), "업체명": r["company"],
                   "소속": r.get("team", ""), "성명": r["name"], "카테고리": r["category"], "점수": r["score"],
                   "합격": "합격" if L.is_pass(r["score"]) else "불합격", "소요(초)": r.get("duration_sec")}
                  for r in rs], hide_index=True)
    st.markdown("**문항별 오답률**")
    st.dataframe([{"문항": s["id"], "카테고리": s["category"], "출제": s["asked"], "오답": s["wrong"],
                   "오답률": f'{round(s["wrong"] / s["asked"] * 100)}%' if s["asked"] else "-"}
                  for s in L.wrong_rates(rs, QUESTIONS)], hide_index=True)
    if st.button("시험 화면으로", key="btn_admin_exit", width="stretch"):
        st.query_params.clear()
        st.rerun()


CSS = """
<style>
.stButton button, .stFormSubmitButton button, .stDownloadButton button { min-height: 52px; font-size: 17px; }
[class*="st-key-opt_"] button, [class*="st-key-cat_"] button,
[class*="st-key-opt_"] button div, [class*="st-key-cat_"] button div,
[class*="st-key-opt_"] button span, [class*="st-key-cat_"] button span { justify-content: flex-start; text-align: left; }
[class*="st-key-opt_"] button p, [class*="st-key-cat_"] button p { text-align: left; font-size: 17px; }
iframe[title="streamlit_js_eval.streamlit_js_eval"] { display: none; }
</style>
"""


def main() -> None:
    st.set_page_config(page_title="기초안전교육 시험", page_icon="⛑", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)
    if not init_storage():
        st.info("저장된 응시 기록을 불러오는 중입니다…")
        return
    ss.setdefault("screen", "start")
    ss.setdefault("user", None)
    if st.query_params.get("admin") == "1":
        screen_admin()
    elif ss.screen != "start" and not ss.user:
        screen_start()
    elif ss.screen == "quiz" and quiz_active():
        screen_quiz()
    elif ss.screen == "result" and ss.get("last_record"):
        screen_result()
    elif ss.screen == "ranking":
        screen_ranking()
    elif ss.screen in ("category", "quiz", "result") and ss.user:
        screen_category()
    else:
        screen_start()
    if not ss.get("store_ok", True) and ss.screen != "result":
        st.caption("⚠ 이 브라우저에서는 기록을 저장할 수 없어, 화면을 닫으면 기록이 사라집니다.")
    flush_save()


main()
