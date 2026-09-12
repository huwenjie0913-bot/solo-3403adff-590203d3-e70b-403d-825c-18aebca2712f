# -*- coding: utf-8 -*-
"""
铸钟内壁车削调音的壳体近似物理模型。

模型概述
--------
将钟体母线离散为若干等弧长环带。对 hum / prime / tierce / quint / nominal
五个分音，用文献中典型的子午向振型（节线圆位置）构造光滑模态形状 w_p(s)，
环向波数取 n = 2, 2, 3, 3, 4（quint 为 (3,1)#，即比 tierce 多一条低节线圆）。

每个环带的能量密度：
  弯曲能  kb ∝ h^3 * (w''^2 + (n^2 w / r)^2) * r
  膜能    km ∝ h   * ((n^2-1) w / r)^2 * r
  动能    m  ∝ h   * w^2 * r

在环带 j 上去料深度 δ（δ < h_j）时：
  dK/K = -(3 kb_j + km_j) / K * δ/h_j
  dM/M = -m_j / M * δ/h_j
  Δf/f = 0.5 * (dK/K - dM/M)

由此得到灵敏度矩阵 S[p][j]（每 mm 去料引起的相对频率变化），
弯曲主导区去料降低频率，质量主导区去料可升高频率，耦合符号自然出现。

既有切削记录用于对每一分音拟合标定系数 alpha_p（带置信度加权的最小二乘），
使预测逐步贴近本钟实测。
"""
import numpy as np

PARTIALS = ["hum", "prime", "tierce", "quint", "nominal"]
PARTIAL_LABEL = {
    "hum": "Hum 哼音",
    "prime": "Prime 基音",
    "tierce": "Tierce 三音",
    "quint": "Quint 五音",
    "nominal": "Nominal 标称",
}
# 环向波数 n（节径数）
CIRC_N = {"hum": 2, "prime": 2, "tierce": 3, "quint": 3, "nominal": 4}
# 理想频率比（相对 prime，纯律）
IDEAL_RATIO = {"hum": 0.5, "prime": 1.0, "tierce": 1.2, "quint": 1.5, "nominal": 2.0}

N_BANDS = 24  # 母线离散的环带数


# ---------------------------------------------------------------- 振型形状
def _gauss(x, mu, sig):
    return np.exp(-((x - mu) / sig) ** 2)


def mode_shape(partial, xi):
    """xi: 0=冠部 -> 1=唇口，返回光滑的子午向模态形状（近似）。"""
    if partial == "hum":      # (2,0) 无节线圆，最大振幅在声弓附近
        w = 1.00 * _gauss(xi, 0.62, 0.42) + 0.35 * _gauss(xi, 0.15, 0.25)
    elif partial == "prime":  # (2,1) 一条节线圆约在腰部
        w = 1.00 * _gauss(xi, 0.78, 0.30) - 0.55 * _gauss(xi, 0.30, 0.28)
    elif partial == "tierce":  # (3,1)
        w = (1.00 * _gauss(xi, 0.55, 0.28) - 0.60 * _gauss(xi, 0.15, 0.18)
             + 0.50 * _gauss(xi, 0.90, 0.15))
    elif partial == "quint":   # (3,1)# 节线圆位置低于 tierce
        w = (1.00 * _gauss(xi, 0.85, 0.22) - 0.70 * _gauss(xi, 0.45, 0.25)
             + 0.40 * _gauss(xi, 0.12, 0.15))
    elif partial == "nominal":  # (4,1) 振幅集中于唇口/声弓
        w = (1.00 * _gauss(xi, 0.92, 0.18) - 0.50 * _gauss(xi, 0.55, 0.25)
             + 0.35 * _gauss(xi, 0.20, 0.18))
    else:
        raise ValueError(partial)
    m = np.max(np.abs(w))
    return w / m if m > 0 else w


# ---------------------------------------------------------------- 母线离散
def discretize_profile(points, n_bands=N_BANDS):
    """
    points: [(z, r, thick), ...] z 自唇口向上(mm)，r 内半径(mm)，thick 壁厚(mm)。
    返回 bands: list of dict(z0, z1, zc, r, thick, xi, ds)。
    环带沿弧长等分，xi 自冠部(0)到唇口(1)。
    """
    pts = sorted(points, key=lambda p: p[0])  # z 自唇口向上
    z = np.array([p[0] for p in pts], float)
    r = np.array([p[1] for p in pts], float)
    h = np.array([p[2] for p in pts], float)
    # 弧长坐标 s（自唇口）
    dz = np.diff(z)
    dr = np.diff(r)
    seg = np.hypot(dz, dr)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = s[-1]
    if total <= 0:
        raise ValueError("profile degenerate")
    # 等弧长节点
    edges_s = np.linspace(0, total, n_bands + 1)
    edges_z = np.interp(edges_s, s, z)
    bands = []
    for j in range(n_bands):
        z0, z1 = edges_z[j], edges_z[j + 1]
        zc = 0.5 * (z0 + z1)
        rc = float(np.interp(zc, z, r))
        hc = float(np.interp(zc, z, h))
        ds = edges_s[j + 1] - edges_s[j]
        # xi: 冠部=0, 唇口=1（z 自唇口向上，故翻转）
        xi = 1.0 - (0.5 * (edges_s[j] + edges_s[j + 1]) / total)
        bands.append(dict(z0=float(z0), z1=float(z1), zc=float(zc),
                          r=rc, thick=hc, xi=float(xi), ds=float(ds)))
    return bands


# ---------------------------------------------------------------- 灵敏度
def sensitivity_matrix(bands):
    """
    返回 S: shape (5, n_bands)，单位：每 mm 去料的相对频率变化 (1/mm)。
    同时返回每分音的模态质量/刚度分布（供诊断）。
    """
    n_b = len(bands)
    xi = np.array([b["xi"] for b in bands])
    r = np.maximum(np.array([b["r"] for b in bands]), 1.0)
    h = np.array([b["thick"] for b in bands])
    ds = np.array([b["ds"] for b in bands])
    dxi = 1.0 / n_b  # 等弧长环带 => xi 等间距（用标量间距，避开数组间距的兼容问题）
    S = np.zeros((len(PARTIALS), n_b))
    detail = {}
    for ip, p in enumerate(PARTIALS):
        n = CIRC_N[p]
        w = mode_shape(p, xi)
        w2 = np.gradient(np.gradient(w, dxi), dxi)  # 二阶导（无量纲弧长坐标）
        # 能量密度（相对量即可）
        kb = h ** 3 * (w2 ** 2 + (n ** 2 * w / np.maximum(r, 1.0) * 1000.0) ** 2 * 1e-6) * r * ds
        km = h * ((n ** 2 - 1) * w / np.maximum(r, 1.0)) ** 2 * r * ds
        m = h * w ** 2 * r * ds
        K = kb.sum() + km.sum()
        M = m.sum()
        if K <= 0 or M <= 0:
            continue
        # 去料 δ：dK = -(3kb + km) δ/h, dM = -m δ/h
        dK = -(3.0 * kb + km) / np.maximum(h, 1e-6)
        dM = -m / np.maximum(h, 1e-6)
        S[ip, :] = 0.5 * (dK / K - dM / M)
        detail[p] = dict(K=float(K), M=float(M),
                         kb=kb.tolist(), km=km.tolist(), m=m.tolist())
    return S, detail


# ---------------------------------------------------------------- 标定
def calibrate(S, cut_records):
    """
    cut_records: list of dict(depths=[...n_bands], before={p:Hz}, after={p:Hz}, confidence=0..1)
    对每一分音拟合 alpha_p：measured_rel ≈ alpha_p * (S_p · depths)
    返回 (alphas dict, info dict)
    """
    alphas, info = {}, {}
    for ip, p in enumerate(PARTIALS):
        num, den, nrec = 0.0, 0.0, 0
        for rec in cut_records:
            d = np.asarray(rec["depths"], float)
            if d.shape[0] != S.shape[1]:
                continue
            b, a = rec["before"].get(p), rec["after"].get(p)
            if not b or not a:
                continue
            w = float(rec.get("confidence", 0.5))
            y = (a - b) / b
            x = float(S[ip, :] @ d)
            num += w * x * y
            den += w * x * x
            nrec += 1
        alpha = num / den if den > 1e-12 else 1.0
        alpha = float(np.clip(alpha, 0.2, 3.0))
        alphas[p] = alpha
        # 可信度：记录数与拟合残差
        res = 0.0
        for rec in cut_records:
            d = np.asarray(rec["depths"], float)
            b, a = rec["before"].get(p), rec["after"].get(p)
            if not b or not a:
                continue
            w = float(rec.get("confidence", 0.5))
            res += w * ((a - b) / b - alpha * float(S[ip, :] @ d)) ** 2
        rms = float(np.sqrt(res / max(nrec, 1)))
        # 经验：3 条以上记录且残差小则可信
        conf = min(1.0, nrec / 3.0) * float(np.exp(-rms * 40.0))
        info[p] = dict(n_records=nrec, rms_rel=rms, confidence=round(conf, 3))
    return alphas, info


# ---------------------------------------------------------------- 预测
def predict(freqs0, S, alphas, depths):
    """
    freqs0: {partial: Hz} 当前频率；depths: 长度 n_bands 的去料深度(mm)。
    返回 {partial: Hz} 预测频率。小变形线性叠加。
    """
    d = np.asarray(depths, float)
    out = {}
    for ip, p in enumerate(PARTIALS):
        f0 = freqs0.get(p)
        if f0 is None:
            out[p] = None
            continue
        rel = alphas[p] * float(S[ip, :] @ d)
        out[p] = f0 * (1.0 + rel)
    return out


def cents(f, f_ref):
    if not f or not f_ref or f <= 0 or f_ref <= 0:
        return None
    return 1200.0 * np.log2(f / f_ref)


# ---------------------------------------------------------------- 检查
def check_plan(bands, depths, freqs0, freqs_pred, targets, min_thick,
               order_guard=0.0):
    """
    返回 (errors, warnings)。
    errors: 壁厚不足等硬约束（按累计去料深度检查）。
    warnings:
      order     —— 振型次序交叉；
      coupling  —— 联动越界：某分音向目标改善，同时另一分音被拖出容差
                  （新越界或越界加剧）；
      deviation —— 无改善联动时，方案本身把某分音推出容差。
    freqs0 为当前（切削前）频率，freqs_pred 为方案预测频率。
    """
    errors, warnings = [], []
    d = np.asarray(depths, float)
    for j, b in enumerate(bands):
        if d[j] > 0 and b["thick"] - d[j] < min_thick - 1e-9:
            errors.append(dict(
                type="wall", band=j, z0=b["z0"], z1=b["z1"],
                msg=f"环带 z={b['z0']:.0f}–{b['z1']:.0f} mm 去料 {d[j]:.2f} mm 后"
                    f"剩余壁厚 {b['thick'] - d[j]:.2f} mm 低于下限 {min_thick:.2f} mm"))
    # 振型次序：五个分音频率应保持 hum<prime<tierce<quint<nominal
    seq = [freqs_pred.get(p) for p in PARTIALS]
    if all(v is not None for v in seq):
        for i in range(len(seq) - 1):
            if seq[i] >= seq[i + 1] * (1.0 - order_guard):
                warnings.append(dict(
                    type="order",
                    msg=f"振型次序交叉风险：{PARTIAL_LABEL[PARTIALS[i]]} "
                        f" ({seq[i]:.2f} Hz) 与 {PARTIAL_LABEL[PARTIALS[i+1]]}"
                        f" ({seq[i + 1]:.2f} Hz) 间距异常"))
    # 偏差前后对比
    dev0, dev1, tol = {}, {}, {}
    for p in PARTIALS:
        t = targets.get(p)
        if not t:
            continue
        tol[p] = t.get("tol_cents", 10.0)
        c0 = cents(freqs0.get(p), t["freq"]) if freqs0.get(p) else None
        c1 = cents(freqs_pred.get(p), t["freq"]) if freqs_pred.get(p) else None
        if c0 is not None:
            dev0[p] = c0
        if c1 is not None:
            dev1[p] = c1
    improved = [p for p in dev1
                if p in dev0 and abs(dev1[p]) < abs(dev0[p]) - 1e-6]
    for p in PARTIALS:
        if p not in dev0 or p not in dev1:
            continue
        tp = tol[p]
        newly_out = abs(dev0[p]) <= tp < abs(dev1[p])
        worse_out = abs(dev0[p]) > tp and abs(dev1[p]) > abs(dev0[p]) + 1e-6
        if not (newly_out or worse_out):
            continue
        if improved:
            warnings.append(dict(
                type="coupling", partial=p, dev_cents=dev1[p],
                msg=(f"联动越界：{PARTIAL_LABEL[p]} 由 {dev0[p]:+.1f} 变为 "
                     f"{dev1[p]:+.1f} 音分（容差 ±{tp:.0f}）—— "
                     f"{'、'.join(PARTIAL_LABEL[q] for q in improved)} "
                     f"改善的联动副作用")))
        else:
            warnings.append(dict(
                type="deviation", partial=p, dev_cents=dev1[p],
                msg=(f"{PARTIAL_LABEL[p]} 被方案推出容差：{dev0[p]:+.1f} → "
                     f"{dev1[p]:+.1f} 音分（容差 ±{tp:.0f}）")))
    return errors, warnings


# ---------------------------------------------------------------- 可达性
def reachability(freqs0, S, alphas, targets, bands, min_thick, max_depth=8.0):
    """
    仅靠去料能否使五个分音全部落入容差？
    用有界最小二乘求最优去料向量，返回每分音最佳可达偏差与缺口。
    """
    from scipy.optimize import lsq_linear
    n_b = len(bands)
    A = np.zeros((len(PARTIALS), n_b))
    b = np.zeros(len(PARTIALS))
    W = np.zeros(len(PARTIALS))
    for ip, p in enumerate(PARTIALS):
        f0 = freqs0.get(p)
        t = targets.get(p)
        if not f0 or not t:
            W[ip] = 0.0
            continue
        A[ip, :] = alphas[p] * S[ip, :]
        b[ip] = (t["freq"] - f0) / f0
        W[ip] = 1.0
    ub = np.array([max(0.0, min(max_depth, bands[j]["thick"] - min_thick))
                   for j in range(n_b)])
    lb = np.zeros(n_b)
    res = lsq_linear(A * W[:, None], b * W, bounds=(lb, ub), max_iter=200)
    d_opt = res.x
    report = {}
    reachable_all = True
    for ip, p in enumerate(PARTIALS):
        f0 = freqs0.get(p)
        t = targets.get(p)
        if not f0 or not t:
            continue
        best = f0 * (1.0 + float(A[ip, :] @ d_opt))
        dev = cents(best, t["freq"])
        ok = dev is not None and abs(dev) <= t.get("tol_cents", 10.0)
        if not ok:
            reachable_all = False
        report[p] = dict(best_freq=round(best, 3), dev_cents=round(dev, 1),
                         reachable=bool(ok),
                         note="" if ok else
                         ("目标高于当前频率，去料通常只能降低该分音，仅靠切削无法达到"
                          if b[ip] > 0 and float(A[ip, :] @ d_opt) < b[ip] * 0.9
                          else "受壁厚/耦合限制，仅靠去料无法达到"))
    return dict(reachable=reachable_all, partials=report,
                depths=d_opt.round(3).tolist())


# ---------------------------------------------------------------- 音名
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_to_freq(name, a4=440.0):
    """如 'E4'、'C#5' -> Hz（十二平均律）。"""
    name = name.strip()
    if not name:
        return None
    i = 1
    if len(name) > 1 and name[1] in "#b":
        i = 2
    n = name[:i]
    try:
        octv = int(name[i:])
    except ValueError:
        return None
    semis = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
    if n[0].upper() not in semis:
        return None
    s = semis[n[0].upper()]
    if len(n) == 2:
        s += 1 if n[1] == "#" else -1
    midi = (octv + 1) * 12 + s
    return a4 * 2.0 ** ((midi - 69) / 12.0)


def freq_to_note(f, a4=440.0):
    if not f or f <= 0:
        return ""
    midi = 69 + 12 * np.log2(f / a4)
    m = int(round(midi))
    return f"{NOTE_NAMES[m % 12]}{m // 12 - 1}"
