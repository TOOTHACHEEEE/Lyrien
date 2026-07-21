"""初筛（filter.py）离线单测：打分、分池、规模上限。"""

from app.pipeline.filter import has_tag_match, prescreen, score_item

TAG_WEIGHTS = {"网络安全": 1.0, "C++": 1.0, "统计学": 0.8}


def _item(title: str, summary: str = "一些摘要", source_weight: float = 1.0) -> dict:
    return {
        "id": 1,
        "title": title,
        "url": "https://example.com/a",
        "summary": summary,
        "source_weight": source_weight,
    }


def test_score_item_matches_tag_weight():
    item = _item("新的网络安全漏洞分析")
    assert score_item(item, TAG_WEIGHTS) == 1.0


def test_score_item_multi_tag_and_source_weight():
    item = _item("C++ 项目的网络安全实践", source_weight=2.0)
    assert score_item(item, TAG_WEIGHTS) == 4.0  # (1.0 + 1.0) * 2.0


def test_score_item_no_summary_penalty():
    item = _item("统计学方法", summary="")
    assert score_item(item, TAG_WEIGHTS) == 0.4  # 0.8 * 0.5


def test_has_tag_match_case_insensitive():
    assert has_tag_match(_item("Modern c++ features"), TAG_WEIGHTS)
    assert not has_tag_match(_item("如何做一道好菜"), TAG_WEIGHTS)


def test_prescreen_splits_pools_and_ranks():
    items = [
        _item("C++ 内存模型详解"),            # 命中，得分 1.0
        _item("网络安全攻防入门"),            # 命中，得分 1.0
        _item("深海鱼类的发光机制"),          # 未命中 → 探索池
        _item("统计学里的贝叶斯推断"),        # 命中，得分 0.8
        _item("城市自行车道设计"),            # 未命中 → 探索池
    ]
    main_pool, explore_pool = prescreen(
        items, main_size=10, explore_size=10, tag_weights=TAG_WEIGHTS
    )

    main_titles = [it["title"] for it in main_pool]
    explore_titles = [it["title"] for it in explore_pool]

    assert len(main_pool) == 3
    assert len(explore_pool) == 2
    assert "深海鱼类的发光机制" in explore_titles
    assert "城市自行车道设计" in explore_titles
    # 命中池按得分降序：0.8 的贝叶斯排最后
    assert main_titles[-1] == "统计学里的贝叶斯推断"


def test_prescreen_respects_pool_size_limits():
    items = [_item(f"C++ 主题文章 {i}") for i in range(20)]
    items += [_item(f"圈外文章 {i}") for i in range(10)]
    main_pool, explore_pool = prescreen(
        items, main_size=5, explore_size=3, tag_weights=TAG_WEIGHTS
    )
    assert len(main_pool) == 5
    assert len(explore_pool) == 3


def test_prescreen_zero_match_falls_back_to_source_quality():
    """标签零命中（如中文标签对英文内容）时，兴趣池按信源质量回填，保证有卡可生。"""
    items = [
        _item("Advanced compiler optimizations", source_weight=1.0),
        _item("New kernel scheduler proposal", source_weight=2.0),
        _item("Random gossip column", source_weight=0.5),
    ]
    main_pool, explore_pool = prescreen(
        items, main_size=2, explore_size=2, tag_weights=TAG_WEIGHTS
    )
    assert [it["title"] for it in main_pool] == [
        "New kernel scheduler proposal",
        "Advanced compiler optimizations",
    ]
    # 回填占用后的剩余条目才进探索池，不重复
    assert [it["title"] for it in explore_pool] == ["Random gossip column"]
