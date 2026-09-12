# -*- coding: utf-8 -*-
"""
多轮复测状态链回归测试 + coupling 联动检查测试。

口径约定（累计/增量统一）：
  - 方案 depths 始终为累计深度；
  - 已执行深度 = 最后一个已完成复测节点之前的车削轮次（效果已含在实测中）；
  - /preview 与轮次预测 = 最新实测 × 剩余深度（累计 − 已执行）；
  - 切削记录只保存本轮新增深度（增量），before 为上一节点实测。

运行：python3 test_rounds.py
"""
import json
import os
import tempfile
import unittest

import numpy as np

import db
db.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="belltune-test-"), "t.db")
import app as appmod  # noqa: E402  (须在设定 DB_PATH 之后导入)
import physics as ph  # noqa: E402

PROFILE = [(0, 330, 40), (20, 322, 42), (60, 300, 38), (120, 268, 30),
           (200, 232, 24), (300, 196, 20), (400, 158, 17), (480, 110, 15),
           (540, 55, 15), (560, 2, 15)]
ASFOUND = dict(hum=168.20, prime=331.05, tierce=402.10, quint=489.90,
               nominal=668.40)
M1 = dict(hum=167.50, prime=330.20, tierce=400.50, quint=489.00, nominal=665.00)
M2 = dict(hum=166.90, prime=329.60, tierce=399.00, quint=488.20, nominal=662.00)
N_BANDS = ph.N_BANDS


def total_depths():
    d = [0.0] * N_BANDS
    for j in range(4):
        d[j] = 2.0          # 唇口 4 个环带各 2 mm，单刀 1 mm => 两轮
    return d


class RoundChainTest(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="belltune-test-"), "t.db")
        db.init_db()
        self.c = appmod.app.test_client()
        self.bid = self._make_bell()
        self.bands = ph.discretize_profile(PROFILE)
        self.S, _ = ph.sensitivity_matrix(self.bands)

    # ----------------------------------------------------------  helpers
    def _make_bell(self):
        c = self.c
        bid = c.post("/api/bells", json={
            "name": "回归测试钟", "pass_depth": 1.0, "lathe_step": 0.1,
            "min_thick": 8.0}).get_json()["id"]
        prime = 329.63
        targets = {p: {"freq": round(prime * ph.IDEAL_RATIO[p], 3),
                       "tol_cents": 10} for p in ph.PARTIALS}
        c.put(f"/api/bells/{bid}/setup", json={
            "name": "回归测试钟", "density": 8800, "a4": 440,
            "pass_depth": 1.0, "lathe_step": 0.1, "min_thick": 8.0,
            "profile": [{"z": z, "r": r, "thick": t} for z, r, t in PROFILE],
            "targets": targets})
        c.post(f"/api/bells/{bid}/measurements", json={
            "round_tag": "as-found",
            "rows": [{"partial": p, "freq": f, "confidence": 0.8}
                     for p, f in ASFOUND.items()]})
        return bid

    def _make_plan(self):
        c = self.c
        pid = c.post(f"/api/bells/{self.bid}/plans",
                     json={"name": "两轮方案", "depths": total_depths()}
                     ).get_json()["id"]
        rounds = c.post(f"/api/plans/{pid}/rounds", json={}).get_json()["rounds"]
        return pid, rounds

    def _alphas(self):
        a, _ = ph.calibrate(self.S, db.cut_records(self.bid))
        return a

    def _assert_freqs(self, got, expect, places=1):
        for p in ph.PARTIALS:
            self.assertAlmostEqual(got[p], expect[p], places=places,
                                   msg=f"{p}: got {got[p]} expect {expect[p]}")

    # ---------------------------------------------------------- 首轮复测
    def test_round1_increment_and_preview_remaining(self):
        pid, rounds = self._make_plan()
        self.assertEqual([r["kind"] for r in rounds],
                         ["cut", "measure", "cut", "measure"])
        cut1 = np.array(rounds[0]["payload"]["depths"])
        self.assertTrue(np.allclose(cut1[:4], 1.0))   # 单刀 1mm

        resp = self.c.post(f"/api/plans/{pid}/rounds/1/measure",
                           json={"measured": M1, "confidence": 0.8}).get_json()
        # 增量 = 第 1 轮车削；已执行 4mm，剩余 4mm
        self.assertTrue(np.allclose(resp["increment"], cut1, atol=1e-3))
        self.assertAlmostEqual(resp["executed_total"], 4.0)
        self.assertAlmostEqual(resp["remaining_total"], 4.0)

        # 切削记录：增量、before=原始实测、after=本轮实测
        recs = db.cut_records(self.bid)
        self.assertEqual(len(recs), 1)
        self.assertTrue(np.allclose(recs[0]["depths"], cut1, atol=1e-4))
        self._assert_freqs(recs[0]["before"], ASFOUND)
        self._assert_freqs(recs[0]["after"], M1)

        # 轮次预测 = M1 × 剩余深度
        remaining = np.array(total_depths()) - cut1
        expect = ph.predict(M1, self.S, self._alphas(), remaining)
        self._assert_freqs(resp["predicted"], expect)

        # 主预览（带 plan_id）：同样只套用剩余深度，不得重复套用总深度
        pv = self.c.post(f"/api/bells/{self.bid}/preview",
                         json={"depths": total_depths(), "plan_id": pid}
                         ).get_json()
        self.assertAlmostEqual(pv["executed_total"], 4.0)
        self.assertTrue(np.allclose(pv["remaining"],
                                    list(remaining), atol=1e-3))
        self._assert_freqs(pv["predicted"], expect)
        # 对照：不带 plan_id 时按完整深度套用（两者必须不同，证明口径生效）
        pv_full = self.c.post(f"/api/bells/{self.bid}/preview",
                              json={"depths": total_depths()}).get_json()
        self.assertNotAlmostEqual(pv["predicted"]["nominal"],
                                  pv_full["predicted"]["nominal"], places=2)

    # ---------------------------------------------------------- 第二轮复测
    def test_round2_increment_not_cumulative(self):
        pid, rounds = self._make_plan()
        cut1 = np.array(rounds[0]["payload"]["depths"])
        cut2 = np.array(rounds[2]["payload"]["depths"])
        self.c.post(f"/api/plans/{pid}/rounds/1/measure",
                    json={"measured": M1, "confidence": 0.8})
        resp2 = self.c.post(f"/api/plans/{pid}/rounds/3/measure",
                            json={"measured": M2, "confidence": 0.8}).get_json()

        # 第二轮增量只含第 2 轮车削，不得重复累计第 1 轮
        self.assertTrue(np.allclose(resp2["increment"], cut2, atol=1e-3))
        self.assertAlmostEqual(resp2["executed_total"], 8.0)
        self.assertAlmostEqual(resp2["remaining_total"], 0.0)

        recs = db.cut_records(self.bid)
        self.assertEqual(len(recs), 2)
        # 记录 2：增量（非累计 2mm）、before=首轮实测 M1、after=M2
        self.assertTrue(np.allclose(recs[1]["depths"], cut2, atol=1e-4))
        self.assertFalse(np.allclose(np.array(recs[1]["depths"]),
                                     cut1 + cut2, atol=1e-4))
        self._assert_freqs(recs[1]["before"], M1)
        self._assert_freqs(recs[1]["after"], M2)

        # 全部执行完毕：剩余为 0，预测应等于最新实测 M2
        self._assert_freqs(resp2["predicted"], M2)

        # 主预览同样收敛到实测值
        pv = self.c.post(f"/api/bells/{self.bid}/preview",
                         json={"depths": total_depths(), "plan_id": pid}
                         ).get_json()
        self.assertAlmostEqual(pv["executed_total"], 8.0)
        self._assert_freqs(pv["predicted"], M2)


class CouplingTest(unittest.TestCase):
    """coupling 检查：仅当某分音改善且另一分音因此被拖出容差时触发。"""

    TG = {"hum": {"freq": 100.0, "tol_cents": 10},
          "prime": {"freq": 200.0, "tol_cents": 10},
          "tierce": {"freq": 240.0, "tol_cents": 10},
          "quint": {"freq": 300.0, "tol_cents": 10},
          "nominal": {"freq": 400.0, "tol_cents": 10}}

    @staticmethod
    def shift(f, cents):
        return f * 2.0 ** (cents / 1200.0)

    def setUp(self):
        self.bands = ph.discretize_profile(PROFILE)
        self.zero = [0.0] * len(self.bands)

    def _warn(self, freqs0, pred):
        return ph.check_plan(self.bands, self.zero, freqs0, pred,
                             self.TG, min_thick=8.0)[1]

    def test_linkage_detected(self):
        # hum 从 +40 音分改善到 0，prime 却被从 0 拖到 +30（新越界）
        f0 = dict(self.TG and {p: t["freq"] for p, t in self.TG.items()})
        f0["hum"] = self.shift(100.0, 40)
        pred = dict(f0)
        pred["hum"] = 100.0
        pred["prime"] = self.shift(200.0, 30)
        w = self._warn(f0, pred)
        coupling = [x for x in w if x["type"] == "coupling"]
        self.assertEqual(len(coupling), 1)
        self.assertEqual(coupling[0]["partial"], "prime")
        self.assertIn("联动", coupling[0]["msg"])
        self.assertFalse(any(x["type"] == "deviation" for x in w))

    def test_no_improvement_no_coupling(self):
        # 没有任何分音改善：只报 deviation，不报 coupling
        f0 = {p: t["freq"] for p, t in self.TG.items()}
        pred = dict(f0)
        pred["prime"] = self.shift(200.0, 30)
        pred["quint"] = self.shift(300.0, -25)
        w = self._warn(f0, pred)
        self.assertFalse(any(x["type"] == "coupling" for x in w))
        self.assertEqual(len([x for x in w if x["type"] == "deviation"]), 2)

    def test_improvement_within_tol_no_warning(self):
        # hum 改善，prime 微动但仍在容差内：无 coupling
        f0 = {p: t["freq"] for p, t in self.TG.items()}
        f0["hum"] = self.shift(100.0, 40)
        pred = dict(f0)
        pred["hum"] = 100.0
        pred["prime"] = self.shift(200.0, 5)
        w = self._warn(f0, pred)
        self.assertFalse(any(x["type"] in ("coupling", "deviation") for x in w))

    def test_worsening_violation_counts(self):
        # hum 改善；prime 本已越界（+20）且被加剧（+35）：也算联动
        f0 = {p: t["freq"] for p, t in self.TG.items()}
        f0["hum"] = self.shift(100.0, 40)
        f0["prime"] = self.shift(200.0, 20)
        pred = dict(f0)
        pred["hum"] = 100.0
        pred["prime"] = self.shift(200.0, 35)
        w = self._warn(f0, pred)
        coupling = [x for x in w if x["type"] == "coupling"]
        self.assertEqual(len(coupling), 1)
        self.assertEqual(coupling[0]["partial"], "prime")


if __name__ == "__main__":
    unittest.main(verbosity=2)
