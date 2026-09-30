"""账单同步字段映射测试。

背景：BSS SDK 的模型属性是 snake_case（product_name/pretax_amount），
而 to_map() 的键才是 PascalCase（ProductName/PretaxAmount）。早期代码用
PascalCase 调 getattr，全部取到 None，导致整表金额=0、产品名=空，
费用页看起来"像没生效"。这些用例把映射钉死，防止回退。
"""
from types import SimpleNamespace

import pytest

from app.models.finance import CloudAccount
from app.services.bill_sync import _dedup_key, _parse_bill_tags, _row_from_item


def _sdk_shaped_item() -> SimpleNamespace:
    """按 DescribeInstanceBill 真实返回构造的 item（snake_case 属性）。"""
    return SimpleNamespace(
        region="中国香港",
        zone="cn-hongkong-b",
        product_code="yundisk",
        product_name="块存储",
        product_detail="云盘",
        subscription_type="PayAsYouGo",
        billing_item="",
        billing_item_code=None,
        item_name="后付费账单",
        pip_code="disk",
        instance_id="d-j6cj0xvvusnj5cpr8tiz",
        nick_name="ros_Disk0_stack_disk_create_and_attachment_2026-07-16",
        resource_group="default resource group",
        cost_unit="未分配",
        tag="key:acs:ros:stackId value:4fa9942c;key:env value:prod",
        pretax_amount=1.765632,
        payment_amount=1.765632,
        cash_amount=1.765632,
        deducted_by_coupons=0.0,
        currency="CNY",
        bill_account_id="1745184130368154",
        # 下面这些 PascalCase 键只存在于 to_map()，getattr 必须取不到
        ProductName="不应被读取",
        PretaxAmount=999.0,
    )


def _account() -> CloudAccount:
    return CloudAccount(id=7, name="测试账号", provider="aliyun")


def test_row_maps_amount_and_product_from_snake_case_attrs():
    row = _row_from_item(_account(), _sdk_shaped_item(), "2026-09-13")

    assert row.account_id == 7
    assert row.billing_cycle == "2026-09"
    assert row.billing_date == "2026-09-13"
    assert row.granularity == "DAILY"
    assert row.provider == "aliyun"
    # 核心回归：金额与产品名必须真的落到行上
    assert row.pretax_amount == pytest.approx(1.7656, abs=1e-4)
    assert row.payment_amount == pytest.approx(1.7656, abs=1e-4)
    assert row.product_name == "块存储"
    assert row.product_detail == "云盘"
    assert row.product_code == "yundisk"
    assert row.instance_id == "d-j6cj0xvvusnj5cpr8tiz"
    assert row.region == "中国香港"
    assert row.zone == "cn-hongkong-b"
    assert row.subscription_type == "PayAsYouGo"
    assert row.currency == "CNY"


def test_row_does_not_read_pascal_case_keys():
    """PascalCase 只是 to_map() 的键，绝不能成为取值来源。"""
    row = _row_from_item(_account(), _sdk_shaped_item(), "2026-09-13")
    assert row.pretax_amount != 999.0
    assert row.product_name != "不应被读取"


def test_row_maps_billing_item_name_from_item_name():
    row = _row_from_item(_account(), _sdk_shaped_item(), "2026-09-13")
    assert row.billing_item_name == "后付费账单"
    # billing_item_code 为空时回落到 pip_code
    assert row.billing_item_code == "disk"


def test_row_maps_resource_name_from_nick_name():
    row = _row_from_item(_account(), _sdk_shaped_item(), "2026-09-13")
    assert row.resource_name.startswith("ros_Disk0_")


def test_parse_bill_tags_from_kv_string():
    item = _sdk_shaped_item()
    row = _row_from_item(_account(), item, "2026-09-13")
    import json
    assert json.loads(row.tags) == [
        {"key": "acs:ros:stackId", "value": "4fa9942c"},
        {"key": "env", "value": "prod"},
    ]


def test_parse_bill_tags_edge_cases():
    assert _parse_bill_tags("") == "[]"
    assert _parse_bill_tags(None) == "[]"
    assert _parse_bill_tags("key:only") == '[{"key": "only", "value": ""}]'
    # 值里带空格不应被切断（key: 取到空格为止，value: 取到段尾）
    assert _parse_bill_tags("key:a value:x y;key:b value:z") == (
        '[{"key": "a", "value": "x y"}, {"key": "b", "value": "z"}]'
    )
    # 段内没有 value: 时值兜底为空字符串，而不是整段原样返回
    assert _parse_bill_tags("key:a;value:lonely") == '[{"key": "a", "value": ""}]'


def test_dedup_key_is_stable_and_distinguishes_instances():
    a, b = _sdk_shaped_item(), _sdk_shaped_item()
    assert _dedup_key(a, 0) == _dedup_key(b, 0)
    b.instance_id = "d-other-instance"
    assert _dedup_key(a, 0) != _dedup_key(b, 0)
    assert _dedup_key(a, 0) != _dedup_key(a, 1)


def test_row_survives_item_with_all_fields_missing():
    """接口偶发返回空 item 时不应抛异常，金额兜底为 0。"""
    row = _row_from_item(_account(), SimpleNamespace(), "2026-09-13")
    assert row.pretax_amount == 0.0
    assert row.product_name == ""
    assert row.currency == "CNY"
    assert row.tags == "[]"
