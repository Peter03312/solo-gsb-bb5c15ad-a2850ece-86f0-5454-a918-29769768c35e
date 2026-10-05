"""三角检验统计：随机猜中概率 p=1/3 下答对次数的二项分布单侧上尾概率。

P(X >= k)，X ~ Binomial(n, 1/3)。
用对数空间 + 递推，避免大 n 时 comb() 中间溢出。
"""

import math

P = 1.0 / 3.0
Q = 2.0 / 3.0


def binomial_tail(n: int, k: int) -> float:
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    # 先算最大项（尾区内），再向上、向下递推求和，防止下溢丢失。
    log_c = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
    log_t = log_c + k * math.log(P) + (n - k) * math.log(Q)
    # 首项已小于浮点可表示量级时，整个尾概率必然也下溢为 0
    if log_t < -745.0:
        return 0.0
    total = math.exp(log_t)
    # T_{i+1} / T_i = (n-i)/(i+1) * p/q
    # i = k..n 各项（T_k 已计入）
    i = k
    cur = math.exp(log_t)
    while i < n:
        cur *= (n - i) / (i + 1) * (P / Q)
        total += cur
        i += 1
    # 极小尾概率截断到 0
    return min(1.0, max(0.0, total))
