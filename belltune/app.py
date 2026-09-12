# -*- coding: utf-8 -*-
"""铸钟内壁车削调音方案系统 — Flask 主应用。本机运行：python3 app.py"""
import json

import numpy as np
from flask import Flask, jsonify, request, Response, render_template

import db
import physics as ph

app = Flask(__name__)
db.init_db()


# ------------------------------------------------------------ 工具
def _model(bell_id):
    """组装当前模型：环带、灵敏度、标定系数、当前频率。"""
    bell = db.q("SELECT * FROM bell WHERE id=?", (bell_id,), one=True)
    if not bell:
        return None
    pts = db.q("SELECT z, r, thick FROM profile_point WHERE bell_id=? ORDER BY seq",
               (bell_id,))
    if len(pts) < 3:
        return dict(bell=bell, bands=[], S=None)
    bands = ph.discretize_profile([(p["z"], p["r"], p["thick"]) for p in pts])
    S, _ = ph.sensitivity_matrix(bands)
    alphas, calinfo = ph.calibrate(S, db.cut_records(bell_id))
    freqs, mconf = db.latest_freqs(bell_id)
    return dict(bell=bell, bands=bands, S=S, alphas=alphas,
                calinfo=calinfo, freqs=freqs, mconf=mconf)


def _targets(bell_id):
    return {t["partial"]: dict(freq=t["freq"], tol_cents=t["tol_cents"])
            for t in db.q("SELECT * FROM target WHERE bell_id=?", (bell_id,))}


def _json_err(msg, code=400):
    return jsonify(dict(error=msg)), code


# ------------------------------------------------------------ 页面
@app.route("/")
def index():
    return render_template("index.html")


# ------------------------------------------------------------ 钟体
@app.route("/api/bells", methods=["GET"])
def list_bells():
    return jsonify(db.q("SELECT * FROM bell ORDER BY created DESC"))


@app.route("/api/bells", methods=["POST"])
def create_bell():
    d = request.get_json(force=True)
    bid = db.execute(
        "INSERT INTO bell(name, density, a4, min_thick, pass_depth, lathe_step, created)"
        " VALUES (?,?,?,?,?,?,?)",
        (d.get("name", "未命名钟"), float(d.get("density", 8800)),
         float(d.get("a4", 440)), float(d.get("min_thick", 8.0)),
         float(d.get("pass_depth", 1.0)), float(d.get("lathe_step", 0.1)), db.now()))
    return jsonify(dict(id=bid))


@app.route("/api/bells/<int:bid>", methods=["GET"])
def get_bell(bid):
    st = db.bell_state(bid)
    if not st:
        return _json_err("not found", 404)
    return jsonify(st)


@app.route("/api/bells/<int:bid>/setup", methods=["PUT"])
def setup_bell(bid):
    """保存母线、材料、目标音高与五分音目标、工艺参数。"""
    d = request.get_json(force=True)
    bell = db.q("SELECT id FROM bell WHERE id=?", (bid,), one=True)
    if not bell:
        return _json_err("not found", 404)
    db.execute("UPDATE bell SET density=?, a4=?, min_thick=?, pass_depth=?,"
               " lathe_step=?, name=? WHERE id=?",
               (float(d.get("density", 8800)), float(d.get("a4", 440)),
                float(d.get("min_thick", 8.0)), float(d.get("pass_depth", 1.0)),
                float(d.get("lathe_step", 0.1)), d.get("name", "未命名钟"), bid))
    db.execute("DELETE FROM profile_point WHERE bell_id=?", (bid,))
    for i, p in enumerate(d.get("profile", [])):
        db.execute("INSERT INTO profile_point(bell_id, seq, z, r, thick)"
                   " VALUES (?,?,?,?,?)",
                   (bid, i, float(p["z"]), float(p["r"]), float(p["thick"])))
    db.execute("DELETE FROM target WHERE bell_id=?", (bid,))
    for p, t in d.get("targets", {}).items():
        if p in ph.PARTIALS and t.get("freq"):
            db.execute("INSERT INTO target(bell_id, partial, freq, tol_cents)"
                       " VALUES (?,?,?,?)",
                       (bid, p, float(t["freq"]), float(t.get("tol_cents", 10))))
    return jsonify(dict(ok=True))


# ------------------------------------------------------------ 测量
@app.route("/api/bells/<int:bid>/measurements", methods=["POST"])
def add_measurements(bid):
    """批量导入一轮敲击测量：{round_tag, rows:[{partial, freq, confidence}]}"""
    d = request.get_json(force=True)
    tag = d.get("round_tag", "as-found")
    n = 0
    for r in d.get("rows", []):
        if r.get("partial") in ph.PARTIALS and r.get("freq"):
            db.execute("INSERT INTO measurement(bell_id, round_tag, partial, freq,"
                       " confidence, created) VALUES (?,?,?,?,?,?)",
                       (bid, tag, r["partial"], float(r["freq"]),
                        float(r.get("confidence", 0.5)), db.now()))
            n += 1
    return jsonify(dict(ok=True, inserted=n))


# ------------------------------------------------------------ 模型
@app.route("/api/bells/<int:bid>/model", methods=["GET"])
def model(bid):
    m = _model(bid)
    if not m:
        return _json_err("not found", 404)
    if m["S"] is None:
        return _json_err("profile incomplete (need >=3 points)")
    targets = _targets(bid)
    devs = {}
    for p in ph.PARTIALS:
        f0 = m["freqs"].get(p)
        t = targets.get(p)
        devs[p] = dict(current=f0, target=t["freq"] if t else None,
                       cents=ph.cents(f0, t["freq"]) if (f0 and t) else None,
                       alpha=m["alphas"][p], calib=m["calinfo"][p],
                       meas_conf=m["mconf"].get(p))
    return jsonify(dict(
        bands=m["bands"],
        partials=ph.PARTIALS,
        S=np.round(m["S"], 8).tolist(),
        alphas=m["alphas"], calinfo=m["calinfo"],
        freqs=m["freqs"], deviations=devs, targets=targets))


@app.route("/api/bells/<int:bid>/preview", methods=["POST"])
def preview(bid):
    """拖动切削量的即时预览：{depths:[...]}"""
    m = _model(bid)
    if not m or m["S"] is None:
        return _json_err("model unavailable")
    d = request.get_json(force=True)
    depths = np.asarray(d.get("depths", [0] * len(m["bands"])), float)
    pred = ph.predict(m["freqs"], m["S"], m["alphas"], depths)
    targets = _targets(bid)
    errors, warnings = ph.check_plan(m["bands"], depths, pred, targets,
                                     m["bell"]["min_thick"])
    out = dict(predicted={p: (round(v, 3) if v else None) for p, v in pred.items()},
               errors=errors, warnings=warnings, partials={})
    for p in ph.PARTIALS:
        t = targets.get(p)
        out["partials"][p] = dict(
            freq=pred.get(p),
            cents=ph.cents(pred.get(p), t["freq"]) if (t and pred.get(p)) else None)
    return jsonify(out)


@app.route("/api/bells/<int:bid>/reachability", methods=["POST"])
def reachable(bid):
    m = _model(bid)
    if not m or m["S"] is None:
        return _json_err("model unavailable")
    d = request.get_json(force=True) or {}
    targets = _targets(bid)
    rep = ph.reachability(m["freqs"], m["S"], m["alphas"], targets, m["bands"],
                          m["bell"]["min_thick"],
                          max_depth=float(d.get("max_depth", 8.0)))
    return jsonify(rep)


# ------------------------------------------------------------ 方案版本
@app.route("/api/bells/<int:bid>/plans", methods=["POST"])
def save_plan(bid):
    d = request.get_json(force=True)
    ver = db.next_version(bid)
    pid = db.execute(
        "INSERT INTO plan(bell_id, version, name, note, depths, status, created)"
        " VALUES (?,?,?,?,?,?,?)",
        (bid, ver, d.get("name", f"方案 v{ver}"), d.get("note", ""),
         json.dumps(d.get("depths", [])), "draft", db.now()))
    return jsonify(dict(id=pid, version=ver))


@app.route("/api/plans/<int:pid>", methods=["GET"])
def get_plan(pid):
    p = db.plan_full(pid)
    if not p:
        return _json_err("not found", 404)
    return jsonify(p)


@app.route("/api/plans/<int:pid>/rounds", methods=["POST"])
def gen_rounds(pid):
    """把方案深度按单刀深度/步进拆成多轮车削 + 复测节点。"""
    p = db.plan_full(pid)
    if not p:
        return _json_err("not found", 404)
    bell = db.q("SELECT * FROM bell WHERE id=?", (p["bell_id"],), one=True)
    depths = np.asarray(p["depths"], float)
    step = max(bell["lathe_step"], 0.01)
    pass_d = max(bell["pass_depth"], step)
    depths = np.round(depths / step) * step  # 量化到车床步进
    db.execute("DELETE FROM round WHERE plan_id=?", (pid,))
    remaining = depths.copy()
    seq = 0
    rnd = 1
    while np.any(remaining > 1e-9):
        cut = np.minimum(remaining, pass_d)
        cut = np.where(cut > 1e-9, cut, 0.0)
        db.execute("INSERT INTO round(plan_id, seq, kind, payload, done)"
                   " VALUES (?,?,?,?,0)",
                   (pid, seq, "cut",
                    json.dumps(dict(round=rnd, depths=cut.round(4).tolist()))))
        seq += 1
        remaining = remaining - cut
        db.execute("INSERT INTO round(plan_id, seq, kind, payload, done)"
                   " VALUES (?,?,?,?,0)",
                   (pid, seq, "measure",
                    json.dumps(dict(round=rnd, measured=None))))
        seq += 1
        rnd += 1
    db.execute("UPDATE plan SET status='active' WHERE id=?", (pid,))
    return jsonify(dict(ok=True, rounds=db.plan_full(pid)["rounds"]))


@app.route("/api/plans/<int:pid>/rounds/<int:seq>/measure", methods=["POST"])
def measure_round(pid, seq):
    """
    录入某复测节点的实测频率 {measured:{partial:Hz}, confidence}。
    写入测量、生成切削记录用于标定，并返回更新后的后续预测。
    """
    p = db.plan_full(pid)
    if not p:
        return _json_err("not found", 404)
    d = request.get_json(force=True)
    measured = {k: float(v) for k, v in d.get("measured", {}).items()
                if k in ph.PARTIALS and v}
    if not measured:
        return _json_err("no measured data")
    conf = float(d.get("confidence", 0.7))
    bid = p["bell_id"]
    # 该节点之前的累计切削
    done_depths = np.zeros(len(p["depths"]))
    for r in p["rounds"]:
        if r["seq"] < seq and r["kind"] == "cut":
            done_depths += np.asarray(r["payload"]["depths"], float)
    before, _ = db.latest_freqs(bid)
    db.execute("UPDATE round SET payload=?, done=1 WHERE plan_id=? AND seq=?",
               (json.dumps(dict(round=(seq // 2) + 1, measured=measured)), pid, seq))
    for k, v in measured.items():
        db.execute("INSERT INTO measurement(bell_id, round_tag, partial, freq,"
                   " confidence, created) VALUES (?,?,?,?,?,?)",
                   (bid, f"plan{pid}-r{seq}", k, v, conf, db.now()))
    if np.any(done_depths > 0) and before:
        db.execute("INSERT INTO cut_record(bell_id, plan_id, round_seq, depths,"
                   " before, after, confidence, created) VALUES (?,?,?,?,?,?,?,?)",
                   (bid, pid, seq, json.dumps(done_depths.round(4).tolist()),
                    json.dumps(before), json.dumps(measured), conf, db.now()))
    # 更新后续预测
    m = _model(bid)
    remaining = np.asarray(p["depths"], float) - done_depths
    pred = ph.predict(measured if len(measured) == len(ph.PARTIALS)
                      else {**m["freqs"], **measured},
                      m["S"], m["alphas"], np.maximum(remaining, 0))
    targets = _targets(bid)
    return jsonify(dict(
        ok=True, alphas=m["alphas"], calinfo=m["calinfo"],
        predicted={k: (round(v, 3) if v else None) for k, v in pred.items()},
        cents={k: (ph.cents(v, targets[k]["freq"]) if (k in targets and v) else None)
               for k, v in pred.items()}))


# ------------------------------------------------------------ 版本比较
@app.route("/api/bells/<int:bid>/compare", methods=["GET"])
def compare(bid):
    a = request.args.get("a", type=int)
    b = request.args.get("b", type=int)
    m = _model(bid)
    if not m or m["S"] is None:
        return _json_err("model unavailable")
    targets = _targets(bid)
    out = []
    for pid in (a, b):
        p = db.plan_full(pid)
        if not p:
            return _json_err(f"plan {pid} not found", 404)
        depths = np.asarray(p["depths"], float)
        pred = ph.predict(m["freqs"], m["S"], m["alphas"], depths)
        errors, warnings = ph.check_plan(m["bands"], depths, pred, targets,
                                         m["bell"]["min_thick"])
        ds_arr = np.array([bd["ds"] for bd in m["bands"]])
        total_removal = round(float((depths * ds_arr).sum()) / 1000.0, 2)
        out.append(dict(
            id=p["id"], version=p["version"], name=p["name"],
            total_removal=total_removal,
            max_depth=round(float(depths.max()) if len(depths) else 0, 3),
            predicted={k: (round(v, 3) if v else None) for k, v in pred.items()},
            cents={k: (ph.cents(v, targets[k]["freq"])
                       if (k in targets and v) else None)
                   for k, v in pred.items()},
            n_errors=len(errors), n_warnings=len(warnings)))
    return jsonify(dict(plans=out))


# ------------------------------------------------------------ 导出
@app.route("/api/plans/<int:pid>/export.json")
def export_json(pid):
    p = db.plan_full(pid)
    if not p:
        return _json_err("not found", 404)
    m = _model(p["bell_id"])
    targets = _targets(p["bell_id"])
    pred = ph.predict(m["freqs"], m["S"], m["alphas"],
                      np.asarray(p["depths"], float)) if m["S"] is not None else {}
    doc = dict(
        plan=p, bell=m["bell"], bands=m["bands"], targets=targets,
        alphas=m["alphas"] if m["S"] is not None else None,
        predicted_final={k: (round(v, 3) if v else None) for k, v in pred.items()},
        exported_at=db.now())
    return Response(json.dumps(doc, ensure_ascii=False, indent=1),
                    mimetype="application/json",
                    headers={"Content-Disposition":
                             f"attachment; filename=plan_{pid}.json"})


@app.route("/api/plans/<int:pid>/export.svg")
def export_svg(pid):
    p = db.plan_full(pid)
    if not p:
        return _json_err("not found", 404)
    m = _model(p["bell_id"])
    if m["S"] is None:
        return _json_err("model unavailable")
    targets = _targets(p["bell_id"])
    svg = render_svg_sheet(p, m, targets)
    return Response(svg, mimetype="image/svg+xml",
                    headers={"Content-Disposition":
                             f"attachment; filename=plan_{pid}.svg"})


def render_svg_sheet(plan, m, targets):
    """生成工艺单 SVG：截面、切削环带标注（轴向位置/深度）、禁止区、复测频率。"""
    bands = m["bands"]
    depths = np.asarray(plan["depths"], float)
    bell = m["bell"]
    pts = db.q("SELECT z, r, thick FROM profile_point WHERE bell_id=? ORDER BY seq",
               (bell["id"],))
    zmax = max(p["z"] for p in pts)
    rmax = max(p["r"] for p in pts)
    W, H = 1000, 760
    ox, oy = 120, 60          # 截面原点（中心线处）
    sc = min(560.0 / max(zmax, 1), 300.0 / max(rmax, 1))
    min_th = bell["min_thick"]

    def X(r):
        return ox + r * sc

    def Y(z):
        return oy + (zmax - z) * sc

    el = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}"'
          f' viewBox="0 0 {W} {H}" font-family="sans-serif">']
    el.append(f'<rect width="{W}" height="{H}" fill="#fbfaf7"/>')
    el.append(f'<text x="20" y="30" font-size="18" font-weight="bold">'
              f'铸钟内壁车削工艺单 — {bell["name"]} / {plan["name"]} (v{plan["version"]})</text>')
    el.append(f'<text x="20" y="50" font-size="11" fill="#666">'
              f'密度 {bell["density"]:.0f} kg/m³ · 单刀 {bell["pass_depth"]} mm · '
              f'步进 {bell["lathe_step"]} mm · 最小壁厚 {min_th} mm</text>')
    # 母线（内外轮廓）
    outer = "M " + " L ".join(f"{X(p['r'] + p['thick']):.1f} {Y(p['z']):.1f}" for p in pts)
    inner = "M " + " L ".join(f"{X(p['r']):.1f} {Y(p['z']):.1f}" for p in pts)
    el.append(f'<path d="{outer}" fill="none" stroke="#8a7a5a" stroke-width="1.5"/>')
    el.append(f'<path d="{inner}" fill="none" stroke="#333" stroke-width="2"/>')
    el.append(f'<line x1="{ox}" y1="{Y(0) - 20}" x2="{ox}" y2="{Y(zmax) + 20}"'
              f' stroke="#bbb" stroke-dasharray="4 4"/>')
    # 禁止区（壁厚 - 最小壁厚 <= 0 的环带，即不允许切削区）+ 切削环带
    for j, bd in enumerate(bands):
        y0, y1 = Y(bd["z1"]), Y(bd["z0"])
        x0, x1 = X(bd["r"]), X(bd["r"] + bd["thick"])
        allow = bd["thick"] - min_th
        if depths[j] > 1e-6:
            el.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}"'
                      f' height="{y1 - y0:.1f}" fill="#d9480f" fill-opacity="0.45"/>')
            el.append(f'<text x="{x1 + 6:.1f}" y="{(y0 + y1) / 2 + 3:.1f}"'
                      f' font-size="9" fill="#a33">z={bd["z0"]:.0f}–{bd["z1"]:.0f}'
                      f'  −{depths[j]:.2f}mm</text>')
        elif allow <= 0.05:
            el.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}"'
                      f' height="{y1 - y0:.1f}" fill="#999" fill-opacity="0.35"/>')
    # 图例
    ly = Y(zmax) + 40
    el.append(f'<rect x="{ox - 60}" y="{ly}" width="12" height="12" fill="#d9480f" fill-opacity="0.45"/>'
              f'<text x="{ox - 44}" y="{ly + 10}" font-size="10">切削环带（标注轴向位置与深度）</text>')
    el.append(f'<rect x="{ox + 200}" y="{ly}" width="12" height="12" fill="#999" fill-opacity="0.35"/>'
              f'<text x="{ox + 216}" y="{ly + 10}" font-size="10">禁止切削区（壁厚不足）</text>')
    # 右侧：轮次与复测频率表
    tx, ty = 560, 90
    el.append(f'<text x="{tx}" y="{ty}" font-size="13" font-weight="bold">车削轮次与复测节点</text>')
    ty += 20
    for r in plan["rounds"]:
        if r["kind"] == "cut":
            ds = np.asarray(r["payload"]["depths"], float)
            tot = float(ds.sum())
            el.append(f'<text x="{tx}" y="{ty}" font-size="10" fill="#222">'
                      f'第{r["payload"]["round"]}轮 车削：总量 {tot:.2f} mm·带，'
                      f'单刀 ≤ {bell["pass_depth"]} mm</text>')
        else:
            meas = r["payload"].get("measured")
            if meas:
                s = "  ".join(f'{k}={v:.2f}' for k, v in meas.items())
                el.append(f'<text x="{tx}" y="{ty}" font-size="10" fill="#076">'
                          f'第{r["payload"]["round"]}轮 复测：{s} Hz</text>')
            else:
                tstr = "  ".join(f'{p}:{targets[p]["freq"]:.2f}' for p in ph.PARTIALS
                                 if p in targets)
                el.append(f'<text x="{tx}" y="{ty}" font-size="10" fill="#06c">'
                          f'第{r["payload"]["round"]}轮 复测（目标 {tstr} Hz）</text>')
        ty += 16
    # 目标频率表
    ty += 14
    el.append(f'<text x="{tx}" y="{ty}" font-size="12" font-weight="bold">目标频率 / 容差</text>')
    ty += 16
    for p_ in ph.PARTIALS:
        t = targets.get(p_)
        if t:
            el.append(f'<text x="{tx}" y="{ty}" font-size="10">'
                      f'{ph.PARTIAL_LABEL[p_]}: {t["freq"]:.2f} Hz '
                      f'±{t["tol_cents"]:.0f} 音分</text>')
            ty += 14
    el.append("</svg>")
    return "\n".join(el)


# ------------------------------------------------------------ 演示数据
@app.route("/api/demo", methods=["POST"])
def demo():
    bid = db.execute(
        "INSERT INTO bell(name, density, a4, min_thick, pass_depth, lathe_step, created)"
        " VALUES ('演示钟 · 青铜 E4', 8800, 440, 8.0, 1.0, 0.1, ?)", (db.now(),))
    profile = [(0, 330, 40), (20, 322, 42), (60, 300, 38), (120, 268, 30),
               (200, 232, 24), (300, 196, 20), (400, 158, 17), (480, 110, 15),
               (540, 55, 15), (560, 2, 15)]
    for i, (z, r, t) in enumerate(profile):
        db.execute("INSERT INTO profile_point(bell_id, seq, z, r, thick)"
                   " VALUES (?,?,?,?,?)", (bid, i, z, r, t))
    prime = ph.note_to_freq("E4", 440.0)
    for p in ph.PARTIALS:
        db.execute("INSERT INTO target(bell_id, partial, freq, tol_cents)"
                   " VALUES (?,?,?,?)",
                   (bid, p, round(prime * ph.IDEAL_RATIO[p], 3), 10))
    asfound = dict(hum=168.20, prime=331.05, tierce=402.10, quint=489.90, nominal=668.40)
    for p, f in asfound.items():
        db.execute("INSERT INTO measurement(bell_id, round_tag, partial, freq,"
                   " confidence, created) VALUES (?,?,?,?,?,?)",
                   (bid, "as-found", p, f, 0.8, db.now()))
    return jsonify(dict(id=bid))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
