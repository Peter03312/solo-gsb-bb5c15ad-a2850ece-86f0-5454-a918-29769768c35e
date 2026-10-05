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

    # --- 2.6 页面可达性 ---
    for path in ("/", "/admin", "/eval", f"/eval/code/{codes[0]}"):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            check(f"页面 {path} → 200", r.status == 200 and b"<!DOCTYPE html>" in r.read(100))

    # ======================================================================
    # 3. 缺席处理：撤回 / 补位（/api/tests/{code}/adjust）
    # ======================================================================
    s, ta = req("POST", "/api/tests", {
        "sample_a": "A-9", "sample_b": "B-9",
        "panelists": [f"A{i}" for i in range(9)],
        "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 9})
    check("创建调整测试(9人,min_valid=9)", s == 200)
    ra = con.execute("SELECT * FROM assignments WHERE test_id="
                     "(SELECT id FROM tests WHERE code=?)",
                     (ta["test_code"],)).fetchall()
    rmap = {r["panelist"]: r for r in ra}

    # 鉴权与时机
    s, b = req("POST", f"/api/tests/{ta['test_code']}/adjust",
               {"token": "wrong", "withdraw_codes": [ra[0]["code"]]})
    check("调整接口错误令牌 403", s == 403, str(b))
    s, b = req("POST", f"/api/tests/NOPE9999/adjust",
               {"token": ta["token"], "withdraw_codes": ["x"]})
    check("调整接口不存在测试 404", s == 404)
    s, b = req("POST", f"/api/tests/{ta['test_code']}/adjust", {"token": ta["token"]})
    check("空调整（无撤回无补位）422", s == 422)

    # A0 作答、A1 弃权；这两人不可撤回
    assert req("POST", f"/api/eval/{rmap['A0']['code']}/submit",
               {"answer": rmap["A0"]["odd_pos"]})[0] == 200
    assert req("POST", f"/api/eval/{rmap['A1']['code']}/submit",
               {"abstain": True})[0] == 200
    s, b = req("POST", f"/api/tests/{ta['test_code']}/adjust",
               {"token": ta["token"],
                "withdraw_codes": [rmap["A0"]["code"], rmap["A1"]["code"]],
                "replacements": ["X1", "X2"]})
    check("已作答/弃权者不可撤回且整次失败 409", s == 409 and "不可撤回" in b["detail"],
          str(b))
    # 原子性：姓名仍不可见（补位未生效）
    s, admin_a = req("GET", f"/api/tests/{ta['test_code']}?token={ta['token']}")
    check("失败后整次回滚（无补位、名单仍 9 人）",
          admin_a["progress"]["total"] == 9 and "X1" not in admin_a["panelists"]
          and "X2" not in admin_a["panelists"])

    # 撤回 A2..A4（均未提交）并补位 N1..N3：9 -> 6 -> 9，满足 min_valid=9
    s, adj = req("POST", f"/api/tests/{ta['test_code']}/adjust",
                 {"token": ta["token"],
                  "withdraw_codes": [rmap[f"A{i}"]["code"] for i in (2, 3, 4)],
                  "replacements": ["N1", "N2", "N3"]})
    check("撤回3人+补位3人 200", s == 200, str(adj))
    check("返回撤回/补位明细数量", len(adj["withdrawn"]) == 3 and len(adj["added"]) == 3)
    check("调整前不泄露新评员杯位/样本",
          all("odd_pos" not in x and "odd_sample" not in x for x in adj["added"]))

    # 有效名单均衡（3/3/3；4/5）
    rows_adj = con.execute(
        "SELECT * FROM assignments WHERE test_id="
        "(SELECT id FROM tests WHERE code=?) AND withdrawn_at IS NULL",
        (ta["test_code"],)).fetchall()
    pc = {p: sum(1 for r in rows_adj if r["odd_pos"] == p) for p in (1, 2, 3)}
    sc = {x: sum(1 for r in rows_adj if r["odd_sample"] == x) for x in ("A", "B")}
    check("撤回+补位后异样杯位置仍均衡 3/3/3", list(pc.values()) == [3, 3, 3], str(pc))
    check("撤回+补位后异样样本均衡 4/5", sorted(sc.values()) == [4, 5], str(sc))
    check("返回的汇总均衡与库内一致",
          adj["balance"]["odd_pos_counts"] == {str(k): v for k, v in pc.items()}
          and adj["balance"]["odd_sample_counts"] == sc)

    # 旧码立即失效：查看与提交都 404
    s, b = req("GET", f"/api/eval/{rmap['A2']['code']}")
    check("撤回后旧码查看 404", s == 404, str(b))
    s, b = req("POST", f"/api/eval/{rmap['A2']['code']}/submit", {"answer": 1})
    check("撤回后旧码提交 404", s == 404)
    # 已作答/已弃权者不受撤回影响，仍可正常查看
    s, ev = req("GET", f"/api/eval/{rmap['A0']['code']}")
    check("已作答者仍有效且保持已提交", s == 200 and ev["submitted"] is True)

    # 新码可用、姓名唯一约束
    s, evn = req("GET", f"/api/eval/{adj['added'][0]['code']}")
    check("补位新码可进入盲评", s == 200 and evn["panelist"] == "N1")
    check("结算前新码不泄露分配", "odd_pos" not in evn and "sample_a" not in evn)
    s, b = req("POST", f"/api/tests/{ta['test_code']}/adjust",
               {"token": ta["token"], "replacements": ["A0"]})
    check("与现有评员重名被拒 422", s == 422 and "不得重用" in b["detail"])
    s, b = req("POST", f"/api/tests/{ta['test_code']}/adjust",
               {"token": ta["token"], "replacements": ["A2"]})
    check("与已撤回者重名被拒 422", s == 422 and "不得重用" in b["detail"])
    s, b = req("POST", f"/api/tests/{ta['test_code']}/adjust",
               {"token": ta["token"], "withdraw_codes": [rmap["A2"]["code"]]})
    check("重复撤回已撤回码 404", s == 404)

    # 只补位（不撤回），9 -> 10 允许
    s, adj2 = req("POST", f"/api/tests/{ta['test_code']}/adjust",
                  {"token": ta["token"], "replacements": ["N4"]})
    check("只补位 200（无 withdrawn）", s == 200 and adj2["withdrawn"] == []
          and adj2["progress"]["total"] == 10)

    # 只撤回但会跌破 min_valid（9）：撤回两人 10 -> 8 → 422，整体无效
    s, b = req("POST", f"/api/tests/{ta['test_code']}/adjust",
               {"token": ta["token"],
                "withdraw_codes": [rmap["A5"]["code"], rmap["A8"]["code"]]})
    check("撤回后有效人数 < min_valid 被拒 422", s == 422 and "最少" in b["detail"],
          str(b))
    s, admin_a = req("GET", f"/api/tests/{ta['test_code']}?token={ta['token']}")
    check("被拒后人数不变 (10)", admin_a["progress"]["total"] == 10)

    # 截止后拒绝调整
    s, td = req("POST", "/api/tests", {
        "sample_a": "D-1", "sample_b": "D-2",
        "panelists": ["d1", "d2"],
        "deadline": int(time.time() * 1000) + 1500, "min_valid": 1})
    rd = con.execute("SELECT * FROM assignments WHERE test_id="
                     "(SELECT id FROM tests WHERE code=?)",
                     (td["test_code"],)).fetchall()
    time.sleep(2.2)
    s, b = req("POST", f"/api/tests/{td['test_code']}/adjust",
               {"token": td["token"], "withdraw_codes": [rd[0]["code"]],
                "replacements": ["nd1"]})
    check("截止后调整被拒 403", s == 403 and "截止" in b["detail"], str(b))

    # 结算后拒绝调整（名单冻结）；结算/回看只统计有效名单
    s, _ = req("POST", f"/api/tests/{td['test_code']}/settle", {"token": td["token"]})
    s, b = req("POST", f"/api/tests/{td['test_code']}/adjust",
               {"token": td["token"], "replacements": ["late1"]})
    check("结算后调整被拒 409", s == 409 and "冻结" in b["detail"], str(b))
    s, rep_d = req("POST", f"/api/tests/{td['test_code']}/settle", {"token": td["token"]})
    check("结算报告名单数=2（撤回未发生，名单未变）", len(rep_d["assignments"]) == 2)

    # 再建一个：撤回未交者后结算，确认撤回者不计入未交/有效
    s, te = req("POST", "/api/tests", {
        "sample_a": "E-1", "sample_b": "E-2",
        "panelists": ["e1", "e2", "e3"],
        "deadline": int(time.time() * 1000) + 2000, "min_valid": 1})
    re_rows = con.execute("SELECT * FROM assignments WHERE test_id="
                          "(SELECT id FROM tests WHERE code=?)",
                          (te["test_code"],)).fetchall()
    # e1 答对，e2 未交将撤回，e3 未交
    assert req("POST", f"/api/eval/{re_rows[0]['code']}/submit",
               {"answer": re_rows[0]["odd_pos"]})[0] == 200
    s, _ = req("POST", f"/api/tests/{te['test_code']}/adjust",
               {"token": te["token"], "withdraw_codes": [re_rows[1]["code"]],
                "replacements": ["e2new"]})
    check("撤回+补位（min_valid=1）200", s == 200)
    time.sleep(2.4)
    s, rep_e = req("POST", f"/api/tests/{te['test_code']}/settle",
                   {"token": te["token"]})
    check("结算只统计有效名单(3人)", len(rep_e["assignments"]) == 3,
          str(len(rep_e["assignments"])))
    names_e = {a["panelist"] for a in rep_e["assignments"]}
    check("回看不含已撤回者、含补位者", "e2" not in names_e and "e2new" in names_e,
          str(names_e))
    # 有效名单=e1(已答)、e2new(未交)、e3(未交)；被撤的 e2 不计入任何统计
    check("撤回者不计未交：未交=2（补位 e2new 与 e3）",
          rep_e["missing_count"] == 2, str(rep_e["missing_count"]))
    check("有效=1、答对=1", rep_e["valid_count"] == 1 and rep_e["correct_count"] == 1)

    # 500 人上限：创建 500 人后再补 1 人 → 422
    s, tf = req("POST", "/api/tests", {
        "sample_a": "F-1", "sample_b": "F-2",
        "panelists": [f"f{i}" for i in range(500)],
        "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 1})
    check("创建 500 人测试", s == 200, str(s))
    s, b = req("POST", f"/api/tests/{tf['test_code']}/adjust",
               {"token": tf["token"], "replacements": ["f500"]})
    check("补位致 501 人被拒 422", s == 422 and "500" in b["detail"], str(b))

    # 并发：答卷与调整竞争同一未交评员——先完成者生效，后到者必败，绝不双生效
    s, tg = req("POST", "/api/tests", {
        "sample_a": "G-1", "sample_b": "G-2",
        "panelists": ["g1", "g2"],
        "deadline": int(time.time() * 1000) + 3600_000, "min_valid": 1})
    rg = con.execute("SELECT * FROM assignments WHERE test_id="
                     "(SELECT id FROM tests WHERE code=?)",
                     (tg["test_code"],)).fetchall()
    barrier = threading.Barrier(2)
    race = {}

    def do_submit():
        barrier.wait()
        race["submit"] = req("POST", f"/api/eval/{rg[0]['code']}/submit",
                             {"answer": rg[0]["odd_pos"]})

    def do_adjust():
        barrier.wait()
        race["adjust"] = req("POST", f"/api/tests/{tg['test_code']}/adjust",
                             {"token": tg["token"],
                              "withdraw_codes": [rg[0]["code"]],
                              "replacements": ["g1new"]})

    t1 = threading.Thread(target=do_submit)
    t2 = threading.Thread(target=do_adjust)
    t1.start(); t2.start()
    t1.join(); t2.join()
    submit_status, submit_body = race["submit"]
    adjust_status, adjust_body = race["adjust"]
    if submit_status == 200:
        # 答卷先完成：旧码已提交，撤回必须因“不可撤回”失败且不得有补位生效
        check("并发-答卷先完成：提交200 / 撤回409",
              adjust_status == 409 and "不可撤回" in adjust_body["detail"],
              f"{submit_status}/{adjust_status} {adjust_body}")
        s, ev_g = req("GET", f"/api/eval/{rg[0]['code']}")
        check("并发后原码仍有效且已提交", s == 200 and ev_g["submitted"] is True)
    else:
        # 调整先完成：旧码被撤，提交必 404；补位 g1new 生效
        check("并发-调整先完成：撤回200 / 提交404",
              adjust_status == 200 and submit_status == 404,
              f"submit={submit_status} adjust={adjust_status}")
        s, ev_g = req("GET", f"/api/eval/{rg[0]['code']}")
        check("并发后旧码已失效", s == 404)
        s, admin_g = req("GET", f"/api/tests/{tg['test_code']}?token={tg['token']}")
        check("并发后补位者在有效名单",
              "g1new" in admin_g["panelists"] and "g1" not in admin_g["panelists"])

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
