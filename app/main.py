"""三角嗅辨盲评工作台 —— FastAPI 后端。

角色：
- 实验负责人：创建测试、凭管理码 + 令牌查看进度、截止后结算并回看分配与答卷。
- 评员：凭盲评码进入，截止前仅可见三杯并提交一次（异样杯位置或弃权）。

安全要点：
- 评员接口任何时刻只返回“三杯随机展示”需要的信息；提交前不暴露 odd_pos/odd_sample。
- 截止判定以服务端时间为准；答卷与结算在同一行锁语义下竞争截止线。
- 管理令牌以 sha256 哈希存储，请求使用恒定时间比较。
"""

import hashlib
import secrets
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .stats import binomial_tail

STATIC_DIR = "static"
ABSTAIN = -1
ACTIVE = "active"
WITHDRAWN = "withdrawn"
MAX_PANELISTS = 500


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def now_ts() -> int:
    return int(time.time())


def new_code(n: int = 10) -> str:
    # 去除易混字符的 URL 安全随机码
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def hash_token(test_code: str, token: str) -> str:
    return hashlib.sha256(f"{test_code}:{token}".encode()).hexdigest()


def verify_admin(row, token: str):
    if not token or not secrets.compare_digest(
        hash_token(row["code"], token), row["token_hash"]
    ):
        raise HTTPException(status_code=403, detail="管理令牌无效")


def require_admin(test_code: str, token: str):
    conn = db.read_conn()
    try:
        row = conn.execute(
            "SELECT * FROM tests WHERE code = ?", (test_code,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="测试不存在")
    verify_admin(row, token)
    return row


def assign_cups(n: int):
    """为 n 名评员分配异样杯位置与异样样本，两者都在全体中尽量均衡。

    - odd_pos：1/2/3 循环打散；odd_sample：A/B 交替，二者独立随机起始。
      => 三杯呈现给评员的映射逐人不同且全体均衡。
    """
    positions = [i % 3 + 1 for i in range(n)]
    samples = ["A" if i % 2 == 0 else "B" for i in range(n)]
    # 随机轮转起始相位并切块打乱，保持计数均衡且顺序不可预测
    shift_p = secrets.randbelow(3)
    positions = positions[shift_p:] + positions[:shift_p]
    shift_s = secrets.randbelow(2)
    samples = samples[shift_s:] + samples[:shift_s]
    order = list(range(n))
    secrets.SystemRandom().shuffle(order)
    result = [None] * n
    for idx, pi in enumerate(order):
        result[pi] = (positions[idx], samples[idx])
    return result


def pick_balanced(c_pos: dict, c_sample: dict):
    """在当前有效名单计数上选 (odd_pos, odd_sample)，使位置与样本计数尽量均衡。

    两维独立贪心：始终挑当前计数最少的那一档；同档随机打破平局。
    逐人插入后，各位置计数极差 ≤ 1、异样样本 A/B 计数极差 ≤ 1。
    """
    pos = min((1, 2, 3), key=lambda p: (c_pos[p], secrets.randbelow(3)))
    sample = min(
        ("A", "B"), key=lambda s: (c_sample[s], secrets.randbelow(2))
    )
    c_pos[pos] += 1
    c_sample[sample] += 1
    return pos, sample


def assignment_public(a) -> dict:
    """评员可见：截止/结算前绝不暴露异样杯对应关系。"""
    return {
        "panelist": a["panelist"],
        "submitted": a["answer"] is not None,
        "abstained": a["answer"] == ABSTAIN,
        "answered_at": a["answered_at"],
    }


def assignment_detail(a) -> dict:
    """负责人回看：含分配与答卷。"""
    return {
        "panelist": a["panelist"],
        "code": a["code"],
        "odd_pos": a["odd_pos"],
        "odd_sample": a["odd_sample"],
        "answer": None if a["answer"] is None else
                  ("弃权" if a["answer"] == ABSTAIN else a["answer"]),
        "correct": (
            None
            if a["answer"] in (None, ABSTAIN)
            else a["answer"] == a["odd_pos"]
        ),
        "answered_at": a["answered_at"],
    }


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class TestCreate(BaseModel):
    sample_a: str = Field(min_length=1, max_length=64)
    sample_b: str = Field(min_length=1, max_length=64)
    panelists: List[str] = Field(min_length=1)
    deadline: int = Field(description="截止时间，Unix 毫秒")
    min_valid: int = Field(ge=1, le=100000)


class AdminAction(BaseModel):
    token: str


class SubmitAnswer(BaseModel):
    answer: Optional[int] = Field(default=None, ge=1, le=3)
    abstain: bool = False


class AdjustRoster(BaseModel):
    """负责人截止前处理缺席：撤回未交盲评码、补位新评员，可单用或合用。"""
    token: str
    withdraw_codes: List[str] = Field(
        default_factory=list, description="要撤回的盲评码，仅未提交者可撤回"
    )
    add_panelists: List[str] = Field(
        default_factory=list, description="补位评员姓名，同一实验姓名不得重用"
    )


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="三角嗅辨盲评工作台", version="1.1", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def http_exc_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail}
    )


@app.get("/api/time")
def server_time():
    return {"now": now_ts() * 1000}


@app.post("/api/tests")
def create_test(body: TestCreate):
    sample_a = body.sample_a.strip()
    sample_b = body.sample_b.strip()
    if not sample_a or not sample_b:
        raise HTTPException(status_code=422, detail="样本编号不能为空")
    if sample_a == sample_b:
        raise HTTPException(status_code=422, detail="两种样本编号必须不同")

    seen, names = set(), []
    for raw in body.panelists:
        name = (raw or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    if not names:
        raise HTTPException(status_code=422, detail="评员名单不能为空")
    if len(names) > MAX_PANELISTS:
        raise HTTPException(status_code=422, detail=f"评员人数不得超过 {MAX_PANELISTS}")

    deadline = body.deadline // 1000  # 毫秒 -> 秒
    if deadline <= now_ts():
        raise HTTPException(status_code=422, detail="截止时间必须晚于当前时间")
    if body.min_valid > len(names):
        raise HTTPException(status_code=422, detail="最少有效答卷数不能多于评员人数")

    test_code = new_code(8)
    token = new_code(16)

    with db.write_tx() as conn:
        cur = conn.execute(
            """INSERT INTO tests
               (code, token_hash, sample_a, sample_b, deadline, min_valid, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (test_code, hash_token(test_code, token),
             sample_a, sample_b, deadline, body.min_valid, now_ts()),
        )
        test_id = cur.lastrowid
        for name, (odd_pos, odd_sample) in zip(names, assign_cups(len(names))):
            conn.execute(
                """INSERT INTO assignments
                   (test_id, panelist, code, odd_pos, odd_sample)
                   VALUES (?, ?, ?, ?, ?)""",
                (test_id, name, new_code(10), odd_pos, odd_sample),
            )

    return {
        "test_code": test_code,
        "token": token,
        "dashboard": f"/admin?test={test_code}&token={token}",
    }


@app.get("/api/tests/{test_code}")
def get_test(test_code: str, token: str):
    """负责人进度视图：截止/结算前不包含任何异样杯对应关系与个人答案。"""
    row = require_admin(test_code, token)
    conn = db.read_conn()
    try:
        assigns = conn.execute(
            "SELECT panelist, code, answer, answered_at FROM assignments "
            "WHERE test_id = ? AND status = ? ORDER BY id",
            (row["id"], ACTIVE),
        ).fetchall()
        withdrawn = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments "
            "WHERE test_id = ? AND status = ?",
            (row["id"], WITHDRAWN),
        ).fetchone()["c"]
    finally:
        conn.close()

    ts = now_ts()
    submitted = sum(1 for a in assigns if a["answer"] is not None)
    abstained = sum(1 for a in assigns if a["answer"] == ABSTAIN)
    valid = sum(1 for a in assigns if a["answer"] not in (None, ABSTAIN))

    return {
        "test_code": row["code"],
        "sample_a": row["sample_a"],
        "sample_b": row["sample_b"],
        "deadline": row["deadline"] * 1000,
        "now": ts * 1000,
        "min_valid": row["min_valid"],
        "settled": row["settled_at"] is not None,
        "withdrawn_count": withdrawn,
        "panelists": [a["panelist"] for a in assigns],
        "codes": [  # 仅负责人可见，用于向评员分发专属盲评链接
            {"panelist": a["panelist"],
             "code": a["code"],
             "url": f"/eval/code/{a['code']}"}
            for a in assigns
        ],
        "progress": {
            "total": len(assigns),
            "submitted": submitted,
            "abstained": abstained,
            "valid": valid,
        },
    }


@app.get("/api/eval/{code}")
def get_assignment(code: str):
    conn = db.read_conn()
    try:
        a = conn.execute(
            "SELECT a.*, t.deadline, t.settled_at, t.sample_a, t.sample_b "
            "FROM assignments a JOIN tests t ON a.test_id = t.id "
            "WHERE a.code = ? AND a.status = ?",
            (code, ACTIVE),
        ).fetchone()
    finally:
        conn.close()
    if a is None:
        raise HTTPException(status_code=404, detail="盲评码无效")

    ts = now_ts()
    past_deadline = ts >= a["deadline"]
    data = {
        "panelist": a["panelist"],
        "deadline": a["deadline"] * 1000,
        "now": ts * 1000,
        "past_deadline": past_deadline,
        "settled": a["settled_at"] is not None,
        "submitted": a["answer"] is not None,
        "abstained": a["answer"] == ABSTAIN,
        "answer": (
            None
            if a["answer"] in (None, ABSTAIN)
            else a["answer"]
        ),
        "answered_at": a["answered_at"],
    }
    # 仅在已结算（结果冻结、负责人已结束盲评）后才向评员公开其本人的分配
    if a["settled_at"] is not None:
        data["odd_pos"] = a["odd_pos"]
        data["odd_sample"] = a["odd_sample"]
        data["sample_a"] = a["sample_a"]
        data["sample_b"] = a["sample_b"]
    return data


@app.post("/api/eval/{code}/submit")
def submit(code: str, body: SubmitAnswer):
    if not body.abstain and body.answer is None:
        raise HTTPException(status_code=422, detail="请选择异样杯位置或选择弃权")

    with db.write_tx() as conn:
        a = conn.execute(
            "SELECT a.*, t.deadline FROM assignments a "
            "JOIN tests t ON a.test_id = t.id "
            "WHERE a.code = ? AND a.status = ?",
            (code, ACTIVE),
        ).fetchone()
        if a is None:
            raise HTTPException(status_code=404, detail="盲评码无效")
        if a["answer"] is not None:
            raise HTTPException(status_code=409, detail="该盲评码已提交，答卷不可修改")
        if now_ts() >= a["deadline"]:
            raise HTTPException(status_code=403, detail="已过截止时间，答卷被拒绝")

        value = ABSTAIN if body.abstain else body.answer
        conn.execute(
            "UPDATE assignments SET answer=?, answered_at=? WHERE id=?",
            (value, now_ts(), a["id"]),
        )
    return {"ok": True, "answer": "弃权" if body.abstain else body.answer}


@app.post("/api/tests/{test_code}/settle")
def settle(test_code: str, body: AdminAction):
    """截止后结算；重复调用返回冻结结果。服务端时间为唯一截止依据。"""
    with db.write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM tests WHERE code = ?", (test_code,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="测试不存在")
        verify_admin(row, body.token)

        ts = now_ts()
        if ts < row["deadline"]:
            raise HTTPException(status_code=409, detail="尚未到截止时间，不能结算")

        if row["settled_at"] is not None:
            frozen = row
        else:
            assigns = conn.execute(
                "SELECT * FROM assignments WHERE test_id = ? AND status = ?",
                (row["id"], ACTIVE),
            ).fetchall()
            valid = sum(1 for a in assigns if a["answer"] not in (None, ABSTAIN))
            correct = sum(
                1 for a in assigns
                if a["answer"] not in (None, ABSTAIN) and a["answer"] == a["odd_pos"]
            )
            p = binomial_tail(valid, correct)
            distinguishable = int(valid >= row["min_valid"] and p <= 0.05)
            conn.execute(
                """UPDATE tests SET settled_at=?, valid_count=?, correct_count=?,
                   p_value=?, distinguishable=? WHERE id=?""",
                (ts, valid, correct, p, distinguishable, row["id"]),
            )
            frozen = conn.execute(
                "SELECT * FROM tests WHERE id = ?", (row["id"],)
            ).fetchone()

        assigns = conn.execute(
            "SELECT * FROM assignments WHERE test_id = ? AND status = ? "
            "ORDER BY id",
            (row["id"], ACTIVE),
        ).fetchall()

    if frozen["valid_count"] < frozen["min_valid"]:
        conclusion = (
            f"有效答卷 {frozen['valid_count']} 份，少于最少要求 "
            f"{frozen['min_valid']} 份：样本不足，无法判定。"
        )
    elif frozen["p_value"] is not None and frozen["p_value"] > 0.05:
        conclusion = (
            f"有效答卷 {frozen['valid_count']} 份、答对 {frozen['correct_count']} 份，"
            f"单侧尾概率 p={frozen['p_value']:.4f} > 0.05：证据未达阈值，"
            "不能判为可辨（差异不显著）。"
        )
    else:
        conclusion = (
            f"有效答卷 {frozen['valid_count']} 份、答对 {frozen['correct_count']} 份，"
            f"单侧尾概率 p={frozen['p_value']:.4f} ≤ 0.05：判为可辨。"
        )

    return {
        "test_code": frozen["code"],
        "sample_a": frozen["sample_a"],
        "sample_b": frozen["sample_b"],
        "deadline": frozen["deadline"] * 1000,
        "settled_at": frozen["settled_at"] * 1000,
        "valid_count": frozen["valid_count"],
        "correct_count": frozen["correct_count"],
        "abstained_count": sum(1 for a in assigns if a["answer"] == ABSTAIN),
        "missing_count": sum(1 for a in assigns if a["answer"] is None),
        "p_value": frozen["p_value"],
        "min_valid": frozen["min_valid"],
        "distinguishable": bool(frozen["distinguishable"]),
        "conclusion": conclusion,
        "assignments": [assignment_detail(a) for a in assigns],
    }


# ---------------------------------------------------------------------------
# 名单调整（撤回 / 补位）
# ---------------------------------------------------------------------------

@app.post("/api/tests/{test_code}/adjust")
def adjust_roster(test_code: str, body: AdjustRoster):
    """截止前处理评员缺席：可只补位、只撤回，或一次操作撤回并补位。

    - 仅未提交（answer IS NULL）者可撤回；已作答或已弃权不可撤回。
    - 撤回后旧码立即无法查看或提交（标记 withdrawn，评员接口按无效码处理）。
    - 补位取得新专属码；同一实验姓名（含已撤回者）不得重用。
    - 已有评员的杯序、异样样本和答卷不变；新分配在当前有效名单上贪心均衡。
    - 调整后有效人数须满足 min_valid ≤ 人数 ≤ 500。
    - 截止或结算后拒绝；任一校验失败整个操作回滚。
    - 与答卷并发时，以实际先拿到写锁并完成的操作为准。
    """
    withdraw_codes = [c.strip() for c in body.withdraw_codes if c and c.strip()]
    if len(withdraw_codes) != len(set(withdraw_codes)):
        raise HTTPException(status_code=422, detail="撤回名单中盲评码重复")

    names, seen_names = [], set()
    for raw in body.add_panelists:
        name = (raw or "").strip()
        if not name:
            continue
        if name in seen_names:
            raise HTTPException(status_code=422, detail=f"补位名单中姓名重复：{name}")
        seen_names.add(name)
        names.append(name)

    if not withdraw_codes and not names:
        raise HTTPException(status_code=422,
                            detail="未提供要撤回的盲评码或补位评员姓名")

    with db.write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM tests WHERE code = ?", (test_code,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="测试不存在")
        verify_admin(row, body.token)

        if row["settled_at"] is not None:
            raise HTTPException(status_code=409,
                                detail="测试已结算，名单已冻结，不能调整")
        if now_ts() >= row["deadline"]:
            raise HTTPException(status_code=409,
                                detail="已过截止时间，不能调整名单")

        # ---- 撤回校验：码属于本实验有效名单，且尚未提交 ----
        withdrawn_rows = []
        for code in withdraw_codes:
            a = conn.execute(
                "SELECT * FROM assignments WHERE test_id = ? AND code = ?",
                (row["id"], code),
            ).fetchone()
            if a is None:
                raise HTTPException(status_code=404, detail=f"盲评码无效：{code}")
            if a["status"] == WITHDRAWN:
                raise HTTPException(
                    status_code=409, detail=f"盲评码 {code} 已撤回，不能重复撤回"
                )
            if a["answer"] is not None:
                kind = "已弃权" if a["answer"] == ABSTAIN else "已作答"
                raise HTTPException(
                    status_code=409,
                    detail=f"评员「{a['panelist']}」{kind}，不可撤回（盲评码 {code}）",
                )
            withdrawn_rows.append(a)

        # ---- 补位校验：同一实验姓名（含已撤回者）不得重用 ----
        if names:
            placeholders = ",".join("?" for _ in names)
            dup_rows = conn.execute(
                f"SELECT panelist FROM assignments WHERE test_id = ? "
                f"AND panelist IN ({placeholders})",
                (row["id"], *names),
            ).fetchall()
            if dup_rows:
                dup = "、".join(r["panelist"] for r in dup_rows)
                raise HTTPException(
                    status_code=422,
                    detail=f"姓名在本实验中已存在（含已撤回者），不得重用：{dup}",
                )

        # ---- 调整后有效人数上下限 ----
        active_count = conn.execute(
            "SELECT COUNT(*) AS c FROM assignments "
            "WHERE test_id = ? AND status = ?",
            (row["id"], ACTIVE),
        ).fetchone()["c"]
        after = active_count - len(withdrawn_rows) + len(names)
        if after < row["min_valid"]:
            raise HTTPException(
                status_code=422,
                detail=f"调整后有效人数 {after} 人，低于最少有效答卷数 "
                       f"{row['min_valid']}，整次操作无效",
            )
        if after > MAX_PANELISTS:
            raise HTTPException(
                status_code=422,
                detail=f"调整后有效人数 {after} 人，超过上限 {MAX_PANELISTS} 人，"
                       "整次操作无效",
            )

        # ---- 执行撤回（旧码保留审计痕迹但立即失效） ----
        for a in withdrawn_rows:
            conn.execute(
                "UPDATE assignments SET status = ? WHERE id = ?",
                (WITHDRAWN, a["id"]),
            )

        # ---- 执行补位：在当前有效名单上逐人贪心均衡 ----
        added = []
        if names:
            c_pos = {1: 0, 2: 0, 3: 0}
            c_sample = {"A": 0, "B": 0}
            for r in conn.execute(
                "SELECT odd_pos, odd_sample, COUNT(*) AS c FROM assignments "
                "WHERE test_id = ? AND status = ? GROUP BY odd_pos, odd_sample",
                (row["id"], ACTIVE),
            ):
                c_pos[r["odd_pos"]] += r["c"]
                c_sample[r["odd_sample"]] += r["c"]

            for name in names:
                odd_pos, odd_sample = pick_balanced(c_pos, c_sample)
                while True:  # 全局 UNIQUE(code)，撞码（极小概率）则重取
                    pcode = new_code(10)
                    if conn.execute(
                        "SELECT 1 FROM assignments WHERE code = ?", (pcode,)
                    ).fetchone() is None:
                        break
                conn.execute(
                    """INSERT INTO assignments
                       (test_id, panelist, code, odd_pos, odd_sample, status)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (row["id"], name, pcode, odd_pos, odd_sample, ACTIVE),
                )
                added.append({
                    "panelist": name,
                    "code": pcode,
                    "url": f"/eval/code/{pcode}",
                })

        active_rows = conn.execute(
            "SELECT answer FROM assignments WHERE test_id = ? AND status = ?",
            (row["id"], ACTIVE),
        ).fetchall()

    submitted = sum(1 for a in active_rows if a["answer"] is not None)
    abstained = sum(1 for a in active_rows if a["answer"] == ABSTAIN)
    valid_answers = sum(
        1 for a in active_rows if a["answer"] not in (None, ABSTAIN)
    )
    return {
        "ok": True,
        "test_code": row["code"],
        "withdrawn": [
            {"panelist": a["panelist"], "code": a["code"]}
            for a in withdrawn_rows
        ],
        "added": added,
        "progress": {
            "total": len(active_rows),
            "submitted": submitted,
            "abstained": abstained,
            "valid": valid_answers,
        },
    }


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(f"{STATIC_DIR}/index.html")


@app.get("/admin")
def admin_page():
    return FileResponse(f"{STATIC_DIR}/admin.html")


@app.get("/eval")
def eval_entry():
    return FileResponse(f"{STATIC_DIR}/eval.html")


@app.get("/eval/code/{code}")
def eval_by_code(code: str):
    return FileResponse(f"{STATIC_DIR}/eval.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
