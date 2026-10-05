"""端到端冒烟测试（仅标准库）：

  python tests/smoke_test.py

会用临时 SQLite 启动真实 uvicorn 子进程，走完整 HTTP 流程。
"""

import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.stats import binomial_tail  # noqa: E402

P, Q = 1 / 3, 2 / 3
FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
# 1. 统计函数：与直接 comb 求和交叉验证
# ---------------------------------------------------------------------------

def ref_tail(n, k):
    return sum(
        math.comb(n, i) * P ** i * Q ** (n - i)
        for i in range(k, n + 1)
    )


for n in range(0, 31):
    for k in range(0, n + 2):
        got = binomial_tail(n, k)
        want = ref_tail(n, min(k, n + 1))
        assert abs(got - want) < 1e-9, (n, k, got, want)

check("二项尾概率与直接求和一致 (0≤n≤30)", True)
check("n=3,k=3 ≈ (1/3)^3", abs(binomial_tail(3, 3) - 1 / 27) < 1e-12,
      binomial_tail(3, 3))
check("k=0 尾概率为 1", binomial_tail(10, 0) == 1.0)
check("k>n 尾概率为 0", binomial_tail(10, 11) == 0.0)
# 经典三角检验数值：n=6 全对或5对的上尾约 0.0178…
check("n=6,k=5 ≈ 0.0178", abs(binomial_tail(6, 5) - 0.01783) < 1e-4,
      binomial_tail(6, 5))
# 大 n 不溢出：正规浮点范围内单调（允许 1 附近 1e-12 舍入误差），次正规以下截断为 0
seq = [binomial_tail(200, k) for k in range(0, 120)]
meaningful = [x for x in seq if x >= 2.3e-308]
check("n=200 正规范围内尾概率随 k 单调不增",
      all(meaningful[i] + 1e-12 >= meaningful[i + 1]
          for i in range(len(meaningful) - 1)))
check("n=200 次正规尾概率截断为 0 且无负值",
      all(x == 0.0 for x in seq if x < 2.3e-308) and min(seq) >= 0)


# ---------------------------------------------------------------------------
# 2. 启动服务，走 HTTP 全流程
# ---------------------------------------------------------------------------

tmpdir = tempfile.mkdtemp(prefix="sniff-test-")
db_path = os.path.join(tmpdir, "test.db")
server_log = open(os.path.join(tmpdir, "server.log"), "w")
port = 8000 + (uuid.uuid4().int % 1000)
base = f"http://127.0.0.1:{port}"

env = dict(os.environ, SNIFF_DB_PATH=db_path)
proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app",
     "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    env=env, stdout=server_log, stderr=subprocess.STDOUT, text=True,
)


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


try:
    # 等待启动
    last_err = None
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/api/time", timeout=2)
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.2)
    else:
        raise RuntimeError(f"服务未启动: {last_err!r}，见 " + server_log.name)

    s, t = req("GET", "/api/time")
    check("GET /api/time", s == 200 and "now" in t)

    # --- 2.1 输入校验 ---
    s, _ = req("POST", "/api/tests", {
        "sample_a": "A1", "sample_b": "A1",
        "panelists": ["x"], "deadline": t["now"] + 60000, "min_valid": 1})
    check("相同样本编号被拒", s == 422)

    s, _ = req("POST", "/api/tests", {
        "sample_a": "A1", "sample_b": "B1",
        "panelists": ["x"], "deadline": t["now"] - 60000, "min_valid": 1})
    check("过去截止时间被拒", s == 422)

    # --- 2.2 均衡性测试（9 人） ---
    s, bal = req("POST", "/api/tests", {
        "sample_a": "S-1", "sample_b": "S-2",
        "panelists": [f"P{i}" for i in range(9)],
        "deadline": t["now"] + 3600_000, "min_valid": 3})
    check("创建 9 人测试", s == 200, str(bal))

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    tid = con.execute("SELECT id FROM tests WHERE code=?", (bal["test_code"],)).fetchone()["id"]
    rows = con.execute("SELECT * FROM assignments WHERE test_id=?", (tid,)).fetchall()
    pos_counts = {p: sum(1 for r in rows if r["odd_pos"] == p) for p in (1, 2, 3)}
    samp_counts = {x: sum(1 for r in rows if r["odd_sample"] == x) for x in ("A", "B")}
    check("异样杯位置均衡 (3/3/3)", list(pos_counts.values()) == [3, 3, 3],
          str(pos_counts))
    check("异样样本均衡 (4/5 或 5/4)", sorted(samp_counts.values()) == [4, 5],
          str(samp_counts))
    codes = [r["code"] for r in rows]
    check("盲评码唯一", len(set(codes)) == 9)

    # 评员接口不得泄露分配
    s, ev = req("GET", f"/api/eval/{codes[0]}")
    check("评员页面可访问", s == 200)
    check("结算前不泄露 odd_pos/odd_sample/样本编号",
          "odd_pos" not in ev and "odd_sample" not in ev and
          "sample_a" not in ev, str(ev))

    # 无效应答 404
    s, _ = req("GET", "/api/eval/NOPE-NOPE")
    check("无效盲评码 404", s == 404)

    # 负责人鉴权
    s, _ = req("GET", f"/api/tests/{bal['test_code']}?token=wrong")
    check("错误令牌 403", s == 403)
    s, admin = req("GET", f"/api/tests/{bal['test_code']}?token={bal['token']}")
    check("负责人可见分发链接", s == 200 and len(admin["codes"]) == 9
          and all(c["code"] for c in admin["codes"]))
    leaked = json.dumps(admin)
    check("进度接口无任何 odd_pos/answer 泄露",
          "odd_pos" not in leaked and '"answer"' not in leaked)

    # 未截止不能结算
    s, _ = req("POST", f"/api/tests/{bal['test_code']}/settle",
               {"token": bal["token"]})
    check("截止前结算被拒 (409)", s == 409)

    # --- 2.3 主测试：6 人，2 秒后截止，min_valid=6 ---
    s, t2 = req("POST", "/api/tests", {
        "sample_a": "X-100", "sample_b": "X-200",
        "panelists": ["甲", "乙", "丙", "丁", "戊", "己"],
        "deadline": int(time.time() * 1000) + 2500, "min_valid": 6})
    check("创建 6 人短截止测试", s == 200)

    rows2 = con.execute("SELECT * FROM assignments WHERE test_id="
                        "(SELECT id FROM tests WHERE code=?)",
                        (t2["test_code"],)).fetchall()
    # 前 3 人答对，第 4 人答错，第 5 人弃权，第 6 人不交
    plan = {0: "correct", 1: "correct", 2: "correct",
            3: "wrong", 4: "abstain", 5: "missing"}
    for i, r in enumerate(rows2):
        if plan[i] == "missing":
            continue
        if plan[i] == "abstain":
            st, _ = req("POST", f"/api/eval/{r['code']}/submit", {"abstain": True})
            check(f"评员{i} 弃权提交 200", st == 200)
        else:
            ans = r["odd_pos"] if plan[i] == "correct" else (
                r["odd_pos"] % 3 + 1)
            st, _ = req("POST", f"/api/eval/{r['code']}/submit", {"answer": ans})
            check(f"评员{i} 选杯提交 200", st == 200)
        # 重复提交
        st2, _ = req("POST", f"/api/eval/{r['code']}/submit", {"answer": 1})
        check(f"评员{i} 二次提交 409", st2 == 409)

    # 等截止
    time.sleep(3.0)

    # 截止后拒答（未交的第 6 人）
    st, body = req("POST", f"/api/eval/{rows2[5]['code']}/submit", {"answer": 1})
    check("截止后答卷被拒 (403)", st == 403, str(body))

    # 结算
    s, rep = req("POST", f"/api/tests/{t2['test_code']}/settle",
                 {"token": t2["token"]})
    check("截止后结算 200", s == 200, str(rep))
    check("有效答卷=4 (弃权/未交不计)", rep["valid_count"] == 4, str(rep["valid_count"]))
    check("答对=3", rep["correct_count"] == 3, str(rep["correct_count"]))
    check("弃权=1/未交=1", rep["abstained_count"] == 1 and rep["missing_count"] == 1)
    check("p = P(X≥3|n=4,p=1/3)≈0.111", abs(rep["p_value"] - 0.111) < 1e-3,
          str(rep["p_value"]))
    # 样本不足（4 < 6）→ 不可辨
    check("有效数不足 → 不可辨", rep["distinguishable"] is False)
    check("结论文案说明样本不足", "样本不足" in rep["conclusion"], rep["conclusion"])

    # 冻结：重复结算返回同一结果
    s, rep2 = req("POST", f"/api/tests/{t2['test_code']}/settle",
                  {"token": t2["token"]})
    check("重复结算返回冻结结果",
          s == 200 and rep2["settled_at"] == rep["settled_at"]
          and rep2["valid_count"] == rep["valid_count"])

    # 结算后评员可见本人分配
    s, ev2 = req("GET", f"/api/eval/{rows2[0]['code']}")
    check("结算后评员可回看 odd_pos", ev2.get("odd_pos") == rows2[0]["odd_pos"])
    check("结算后评员可见样本编号", ev2.get("sample_a") == "X-100")

    # --- 2.4 显著场景：min_valid=4，4 人全对 → p=(1/3)^4≈0.0123 → 可辨 ---
    s, t3 = req("POST", "/api/tests", {
        "sample_a": "K-1", "sample_b": "K-2",
        "panelists": ["q1", "q2", "q3", "q4"],
        "deadline": int(time.time() * 1000) + 2000, "min_valid": 4})
    rows3 = con.execute("SELECT * FROM assignments WHERE test_id="
                        "(SELECT id FROM tests WHERE code=?)",
                        (t3["test_code"],)).fetchall()
    for r in rows3:
        st, _ = req("POST", f"/api/eval/{r['code']}/submit",
                    {"answer": r["odd_pos"]})
        assert st == 200
    time.sleep(2.6)
    s, rep3 = req("POST", f"/api/tests/{t3['test_code']}/settle",
                  {"token": t3["token"]})
    check("4/4 全对 p≈0.0123", abs(rep3["p_value"] - 1 / 81) < 1e-9,
          str(rep3["p_value"]))
    check("4/4 全对且达标 → 判为可辨", rep3["distinguishable"] is True,
          rep3["conclusion"])
    check("回看含全部分配与答卷", len(rep3["assignments"]) == 4
          and all(a["correct"] is True for a in rep3["assignments"]))

    # --- 2.5 零有效答卷边界：全员未交 → p=1.0、样本不足、不可辨 ---
    s, t4 = req("POST", "/api/tests", {
        "sample_a": "Z-1", "sample_b": "Z-2",
        "panelists": ["z1", "z2", "z3"],
        "deadline": int(time.time() * 1000) + 2000, "min_valid": 2})
    assert s == 200
    time.sleep(2.6)
    s, rep4 = req("POST", f"/api/tests/{t4['test_code']}/settle",
                  {"token": t4["token"]})
    check("零答卷结算 200", s == 200)
    check("零有效答卷 valid=0/correct=0/p=1.0",
          rep4["valid_count"] == 0 and rep4["correct_count"] == 0
          and rep4["p_value"] == 1.0, str(rep4))
    check("零有效答卷判为样本不足而非证据未达阈值",
          rep4["distinguishable"] is False and "样本不足" in rep4["conclusion"],
          rep4["conclusion"])

    # --- 2.7 名单调整：撤回 / 补位（复用 9 人均衡测试 bal，截止尚远、无人提交） ---
    # 先记录调整前分配（已有评员的杯序/异样样本必须保持不变）
    before = con.execute(
        "SELECT panelist, odd_pos, odd_sample FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) ORDER BY panelist",
        (bal["test_code"],)).fetchall()
    before_map = {r["panelist"]: (r["odd_pos"], r["odd_sample"]) for r in before}

    # 只补位：+1 人，补到当前计数最缺的位置与样本
    s, add1 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                  {"token": bal["token"], "add_panelists": ["P9"]})
    check("只补位 1 人 200", s == 200, str(add1))
    check("补位返回新专属码",
          len(add1["added"]) == 1 and add1["added"][0]["panelist"] == "P9"
          and add1["added"][0]["code"] and add1["added"][0]["url"])
    check("补位后有效名单=10", add1["progress"]["total"] == 10)

    # 一次操作撤回 3 人并补位 2 人：10-3+2=9，位置计数应恢复 3/3/3
    withdraw3 = [codes[6], codes[7], codes[8]]
    s, adj = req("POST", f"/api/tests/{bal['test_code']}/adjust", {
        "token": bal["token"],
        "withdraw_codes": withdraw3,
        "add_panelists": ["R1", "R2"]})
    check("撤回3补位2 一次操作 200", s == 200, str(adj))
    check("撤回名单含 3 人", len(adj["withdrawn"]) == 3)
    check("补位名单含 2 人",
          [a["panelist"] for a in adj["added"]] == ["R1", "R2"])
    check("调整后有效名单=9", adj["progress"]["total"] == 9)

    after = con.execute(
        "SELECT * FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) AND status='active'",
        (bal["test_code"],)).fetchall()
    pc = {p: sum(1 for r in after if r["odd_pos"] == p) for p in (1, 2, 3)}
    sc = {x: sum(1 for r in after if r["odd_sample"] == x) for x in ("A", "B")}
    check("撤回+补位后异样杯位置仍均衡 3/3/3", list(pc.values()) == [3, 3, 3],
          str(pc))
    check("撤回+补位后异样样本仍均衡 (4/5 或 5/4)",
          sorted(sc.values()) == [4, 5], str(sc))
    check("已有评员的杯序/异样样本不变",
          all(before_map[r["panelist"]] == (r["odd_pos"], r["odd_sample"])
              for r in after if r["panelist"] in before_map))
    check("补位者取得全新唯一码",
          all(r["code"] not in codes for r in after if r["panelist"] in ("R1", "R2")))

    # 撤回后的旧码立即无法查看或提交
    st, _ = req("GET", f"/api/eval/{codes[6]}")
    check("已撤回旧码查看 → 404", st == 404)
    st, _ = req("POST", f"/api/eval/{codes[6]}/submit", {"answer": 1})
    check("已撤回旧码提交 → 404", st == 404)

    # 进度接口只统计有效名单，并给出 withdrawn_count
    s, admin2 = req("GET", f"/api/tests/{bal['test_code']}?token={bal['token']}")
    check("进度接口有效名单=9、已撤回=3",
          s == 200 and admin2["progress"]["total"] == 9
          and admin2["withdrawn_count"] == 3, str(admin2.get("progress")))
    check("分发名单不含已撤回者",
          sorted(c["panelist"] for c in admin2["codes"]
                 if c["panelist"] in (f"P{i}" for i in (6, 7, 8))) == [])

    # 已作答 / 已弃权者不可撤回：单独建一个 3 人测试
    s, aw = req("POST", "/api/tests", {
        "sample_a": "W-1", "sample_b": "W-2",
        "panelists": ["w1", "w2", "w3"],
        "deadline": t["now"] + 3600_000, "min_valid": 1})
    assert s == 200
    aw_rows = con.execute(
        "SELECT * FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?)", (aw["test_code"],)).fetchall()
    req("POST", f"/api/eval/{aw_rows[0]['code']}/submit", {"answer": 1})
    req("POST", f"/api/eval/{aw_rows[1]['code']}/submit", {"abstain": True})
    st, b1 = req("POST", f"/api/tests/{aw['test_code']}/adjust", {
        "token": aw["token"], "withdraw_codes": [aw_rows[0]["code"]]})
    check("已作答者不可撤回 (409)", st == 409 and "已作答" in b1["detail"],
          str(b1))
    st, b2 = req("POST", f"/api/tests/{aw['test_code']}/adjust", {
        "token": aw["token"], "withdraw_codes": [aw_rows[1]["code"]]})
    check("已弃权者不可撤回 (409)", st == 409 and "已弃权" in b2["detail"],
          str(b2))

    # 姓名不得重用（与有效名单、已撤回者都冲突）
    st, b3 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": bal["token"], "add_panelists": ["R1"]})
    check("与有效名单重名被拒 (422)", st == 422 and "重用" in b3["detail"])
    st, b4 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": bal["token"], "add_panelists": ["P7"]})
    check("与已撤回者重名被拒 (422)", st == 422 and "重用" in b4["detail"])
    st, b5 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": bal["token"],
                  "add_panelists": ["dup", "dup"]})
    check("补位名单内部重名被拒 (422)", st == 422)
    st, b6 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": bal["token"],
                  "withdraw_codes": [codes[0], codes[0]]})
    check("撤回名单内部重复被拒 (422)", st == 422)
    st, b7 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": bal["token"]})
    check("空操作被拒 (422)", st == 422)
    st, b8 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": bal["token"], "withdraw_codes": ["NOPE-NOPE"]})
    check("撤回无效盲评码 → 404", st == 404)
    st, b9 = req("POST", f"/api/tests/{bal['test_code']}/adjust",
                 {"token": "wrong", "add_panelists": ["x"]})
    check("调整接口错误令牌 403", st == 403)

    # 下限：新建 min_valid=4 的 4 人测试，撤回 1 人 → 3 < 4，整次操作无效
    s, mn = req("POST", "/api/tests", {
        "sample_a": "M-1", "sample_b": "M-2",
        "panelists": ["m1", "m2", "m3", "m4"],
        "deadline": t["now"] + 3600_000, "min_valid": 4})
    assert s == 200
    mn_rows = con.execute(
        "SELECT code FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) ORDER BY id",
        (mn["test_code"],)).fetchall()
    st, low = req("POST", f"/api/tests/{mn['test_code']}/adjust", {
        "token": mn["token"], "withdraw_codes": [mn_rows[0]["code"]]})
    check("低于最少有效人数整次拒绝 (422)",
          st == 422 and "低于" in low["detail"], str(low))
    mn_left = con.execute(
        "SELECT COUNT(*) AS c FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) AND status='active'",
        (mn["test_code"],)).fetchone()["c"]
    check("拒绝后名单不变（仍 4 人有效 / 0 撤回）", mn_left == 4, str(mn_left))
    st, _ = req("GET", f"/api/eval/{mn_rows[0]['code']}")
    check("被回滚的撤回码仍可正常查看", st == 200)
    # 撤回并同数补位使人数不变 → 允许
    st, keep = req("POST", f"/api/tests/{mn['test_code']}/adjust", {
        "token": mn["token"],
        "withdraw_codes": [mn_rows[0]["code"]],
        "add_panelists": ["m5"]})
    check("撤回1补位1 人数仍=4 允许",
          st == 200 and keep["progress"]["total"] == 4, str(st))

    # 上限：新建 2 人测试（min_valid=1），直接补到 500 允许，再补 1 人拒绝
    s, cap = req("POST", "/api/tests", {
        "sample_a": "C-1", "sample_b": "C-2",
        "panelists": ["c1", "c2"],
        "deadline": t["now"] + 3600_000, "min_valid": 1})
    assert s == 200
    st, cap498 = req("POST", f"/api/tests/{cap['test_code']}/adjust",
                     {"token": cap["token"],
                      "add_panelists": [f"N{i}" for i in range(498)]})
    check("补位至 500 人允许 (200)",
          st == 200 and cap498["progress"]["total"] == 500, str(st))
    st, cap501 = req("POST", f"/api/tests/{cap['test_code']}/adjust",
                     {"token": cap["token"], "add_panelists": ["N498"]})
    check("补位超过 500 人拒绝 (422)", st == 422 and "上限" in cap501["detail"],
          str(cap501))
    cap_rows = con.execute(
        "SELECT odd_pos FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) AND status='active'",
        (cap["test_code"],)).fetchall()
    cap_pc = {p: sum(1 for r in cap_rows if r["odd_pos"] == p)
              for p in (1, 2, 3)}
    check("补位到 500 人位置计数极差 ≤ 1",
          max(cap_pc.values()) - min(cap_pc.values()) <= 1, str(cap_pc))

    # 截止后 / 结算后拒绝调整（复用已结算的 t2）
    st, da = req("POST", f"/api/tests/{t2['test_code']}/adjust",
                 {"token": t2["token"], "add_panelists": ["late"]})
    check("已结算测试调整被拒 (409)", st == 409)

    # 截止（未结算）同样拒绝：建 1 人短截止测试并等过截止
    s, dl = req("POST", "/api/tests", {
        "sample_a": "D-1", "sample_b": "D-2",
        "panelists": ["d1"],
        "deadline": int(time.time() * 1000) + 1500, "min_valid": 1})
    assert s == 200
    time.sleep(2.0)
    st, dle = req("POST", f"/api/tests/{dl['test_code']}/adjust",
                  {"token": dl["token"], "add_panelists": ["d2"]})
    check("截止后未结算也拒绝调整 (409)", st == 409 and "截止" in dle["detail"],
          str(dle))

    # 结算只统计有效名单：新建 4 人，1 答对 1 弃权 1 未交 1 撤回
    s, ex = req("POST", "/api/tests", {
        "sample_a": "E-1", "sample_b": "E-2",
        "panelists": ["e1", "e2", "e3", "e4"],
        "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 1})
    assert s == 200
    ex_rows = con.execute(
        "SELECT * FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) ORDER BY id",
        (ex["test_code"],)).fetchall()
    req("POST", f"/api/eval/{ex_rows[0]['code']}/submit",
        {"answer": ex_rows[0]["odd_pos"]})      # 答对
    req("POST", f"/api/eval/{ex_rows[1]['code']}/submit", {"abstain": True})
    # e3 未交；e4 未交且在截止前被撤回
    st, _ = req("POST", f"/api/tests/{ex['test_code']}/adjust", {
        "token": ex["token"],
        "withdraw_codes": [ex_rows[3]["code"]],
        "add_panelists": ["e5"]})                 # e5 补位后也未交
    assert st == 200
    # 直接在库里把截止时间改到过去以便立即结算
    con.execute("UPDATE tests SET deadline=? WHERE code=?",
                (int(time.time()) - 10, ex["test_code"]))
    con.commit()
    s, exr = req("POST", f"/api/tests/{ex['test_code']}/settle",
                 {"token": ex["token"]})
    check("结算排除已撤回：有效名单 4 人",
          exr["valid_count"] == 1 and exr["abstained_count"] == 1
          and exr["missing_count"] == 2, str((exr["valid_count"],
                                              exr["abstained_count"],
                                              exr["missing_count"])))
    check("结算回看不含已撤回评员 e4",
          all(a["panelist"] != "e4" for a in exr["assignments"])
          and {a["panelist"] for a in exr["assignments"]}
              == {"e1", "e2", "e3", "e5"})

    # --- 2.8 并发：同一评员码上「撤回」与「答卷」竞争，按实际先完成者判定 ---
    now_s, _ = req("GET", "/api/time")
    s, ct = req("POST", "/api/tests", {
        "sample_a": "F-1", "sample_b": "F-2",
        "panelists": [f"c{i}" for i in range(40)],
        "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 1})
    assert s == 200
    ct_rows = con.execute(
        "SELECT code FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) ORDER BY id",
        (ct["test_code"],)).fetchall()

    def race_round(c):
        result = {}
        barrier = threading.Barrier(2)

        def do_submit():
            barrier.wait()
            stt, _ = req("POST", f"/api/eval/{c}/submit", {"answer": 1})
            result["submit"] = stt

        def do_withdraw():
            barrier.wait()
            stt, _ = req("POST", f"/api/tests/{ct['test_code']}/adjust",
                         {"token": ct["token"], "withdraw_codes": [c]})
            result["withdraw"] = stt

        t1 = threading.Thread(target=do_submit)
        t2 = threading.Thread(target=do_withdraw)
        t1.start(); t2.start(); t1.join(); t2.join()
        return result

    n_submit_win = n_withdraw_win = 0
    race_ok = no_5xx = True
    for i in range(20):  # 20 个不同的码；最坏全撤回也仍有 20 名有效评员
        c = ct_rows[i]["code"]
        r = race_round(c)
        row = con.execute(
            "SELECT status, answer FROM assignments WHERE code=?", (c,)
        ).fetchone()
        # 答卷先完成：submit=200、withdraw=409（已作答不可撤回），active 且有答卷
        if r["submit"] == 200 and r["withdraw"] == 409:
            if not (row["status"] == "active" and row["answer"] == 1):
                race_ok = False
            n_submit_win += 1
        # 撤回先完成：withdraw=200、submit=404（旧码立即失效），withdrawn 且无答卷
        elif r["withdraw"] == 200 and r["submit"] == 404:
            if not (row["status"] == "withdrawn" and row["answer"] is None):
                race_ok = False
            n_withdraw_win += 1
        else:
            race_ok = False  # 不允许双赢 / 双输 / 其他状态
        if r["submit"] >= 500 or r["withdraw"] >= 500:
            no_5xx = False

    check("并发：撤回与答卷结果互补、数据库状态与胜出方一致，无脏中间态",
          race_ok and n_submit_win + n_withdraw_win == 20,
          f"答卷先完成 {n_submit_win} 轮，撤回先完成 {n_withdraw_win} 轮")
    check("并发过程中无 5xx 服务端错误", no_5xx)

    # --- 2.6 页面可达性 ---
    for path in ("/", "/admin", "/eval", f"/eval/code/{codes[0]}"):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            check(f"页面 {path} → 200", r.status == 200 and b"<!DOCTYPE html>" in r.read(100))

    con.close()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    server_log.close()

print()
if FAILS:
    print(f"失败 {len(FAILS)} 项: {FAILS}")
    sys.exit(1)
print("全部通过 ✓")
