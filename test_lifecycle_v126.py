# -*- coding: utf-8 -*-
"""v1.26.0 自测：注销归档 + 重新注册 + 子网写法统一。"""
import datetime
import json
import os
import tempfile

import subnet_archive as A
import tg_group_digest as TGD

QUIET = lambda *a, **k: None


def _arc(sn, name):
    return {"sn": sn, "name": name, "biz": "测试业务", "url": "", "github": "",
            "status": "active", "lifecycle": "active",
            "created_at": "2026-11-01 10:00", "updated_at": "2026-11-01 10:00",
            "days": {}, "weak_days": {}, "reports": [], "buyback_wallets": []}


def test_lifecycle_and_rebuild():
    tmp = tempfile.mkdtemp(prefix="snlifecycle_")
    arc = os.path.join(tmp, "archive")
    os.makedirs(arc, exist_ok=True)
    cfg = {"archive_dir": arc}

    kb = {"updated_at": "2026-11-22",
          "subnets": {"1": {"name": "Root", "desc": "", "aliases": []}},
          "deregistered": {"41": {"name": "Almanac", "status": "deregistered",
                                  "deregistered_on": "2026-11-22"}}}
    old_load = A.load_kb
    A.load_kb = lambda path=None: kb
    try:
        A.save_archive(arc, _arc(41, "Almanac"))
        A.save_md(arc, A.load_archive(arc, 41))
        A.refresh_index(arc)
        assert "41" in A.load_all_index(arc)["subnets"]

        assert A.is_retired_sn(41, kb) is True
        assert A.resolve_sn("41") is None

        # 投研/社区落档在注销后必须拒绝，且不能重建活动档
        for fn in (lambda: A.attach_research(cfg, 41, "2026-11-22", "t", "body"),
                   lambda: A.attach_community_digest(cfg, "2026-11-22", 41, "x"),
                   lambda: A.add_manual_wallet(arc, 41, "5" + "a" * 47)):
            try:
                fn()
                raise AssertionError("注销子网仍可写入档案")
            except RuntimeError:
                pass

        retired = A.retire_archives_from_kb(arc, kb=kb, log=QUIET)
        assert len(retired) == 1 and retired[0]["sn"] == 41
        assert not os.path.exists(A.archive_path(arc, 41))
        rp = A.find_retired_archive(arc, 41)
        assert rp and "2026-11-22" in os.path.basename(rp)
        with open(os.path.join(rp, "archive.json"), encoding="utf-8") as f:
            rarc = json.load(f)
        assert rarc["status"] == "deregistered"
        assert rarc["deregistered_on"] == "2026-11-22"
        assert A.load_archive(arc, 41) is None
        assert "41" not in A.refresh_index(arc)["subnets"]
        with open(os.path.join(arc, A.RETIRED_INDEX), encoding="utf-8") as f:
            assert "41" in json.load(f)["subnets"]
        # 幂等
        assert A.retire_archives_from_kb(arc, kb=kb, log=QUIET) == []

        # 历史导出也不能把注销子网建回活动目录
        msg = os.path.join(tmp, "dtao_2026-11-01.1.json")
        with open(msg, "w", encoding="utf-8") as f:
            json.dump({"messages": [{"id": "x1", "timestamp": "2026-11-01T10:00:00Z",
                                     "author": {"nickname": "a"},
                                     "content": "SN41 Almanac buyback", "reactions": []}]}, f)
        r = A.archive_files(cfg, [msg], kb=kb, log=QUIET)
        assert 41 in r["skipped_retired"]
        assert not os.path.exists(A.archive_path(arc, 41))

        # 同号重新注册：旧档保留，新档从零开始
        kb2 = {"updated_at": "2026-11-23",
               "subnets": {"41": {"name": "Almanac2", "desc": "new", "zh": "新业务",
                                  "url": "", "github": "", "aliases": ["Almanac"]}},
               "deregistered": {}}
        A.load_kb = lambda path=None: kb2
        assert A.resolve_sn("41") == 41
        fresh = A.create_fresh_archive(arc, 41, kb_rec=kb2["subnets"]["41"], log=QUIET)
        assert fresh and fresh["name"] == "Almanac2"
        assert fresh["reports"] == [] and fresh["days"] == {}
        assert os.path.exists(rp)           # 旧档没被覆盖
        assert "41" in A.load_all_index(arc)["subnets"]
        # 新档可以正常投研落档
        A.attach_research(cfg, 41, "2026-11-23", "title", "**🗓 重要事件时间线**\n- 2026-11-23 · ✅已验证 test",
                          log=QUIET)
        assert len(A.load_archive(arc, 41)["reports"]) == 1
    finally:
        A.load_kb = old_load


def test_sn_name_normalize():
    names = {41: "Almanac", 53: "engy"}
    s = ("**SN AlMANAC** 提到 **Almanac**，另有 SN41（Almanac）；"
         "SN53 engy 正常，但 SN53 单独出现也要补全")
    out = TGD.normalize_sn_names(s, names)
    assert "SN AlMANAC" not in out
    assert "Almanac 子网" not in out
    assert out.count("SN41 Almanac") == 3, out
    assert out.count("SN53 engy") == 2, out
    # 已正确的写法不被二次改写
    once = TGD.normalize_sn_names("SN41 Almanac 和 SN53 engy", names)
    assert once == "SN41 Almanac 和 SN53 engy", once


if __name__ == "__main__":
    test_lifecycle_and_rebuild()
    test_sn_name_normalize()
    print("v1.26.x lifecycle tests: 2/2 passed")
