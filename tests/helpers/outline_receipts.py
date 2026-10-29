"""仅为临时测试工作区建立完整、同源的模拟五项通过记录。"""
import json

from src.services.outline_pre_mini_review_receipt import make_receipt


def install_review_receipt(service, *, gaps=(), premise="虚构", genre="mystery"):
    source = {k: v for k, v in service.read_outline("test").items() if k != "mini_plan"}
    source.update(premise=premise, genre=genre)
    receipt = make_receipt(source, list(gaps), evidence=[
        {"stage": stage, "source": "isolated_test_response"}
        for stage in ("world", "characters", "plot", "volume_plans", "cross")
    ])
    path = service._outline_dir("test") / "pre_mini_review_receipt.json"
    path.write_text(json.dumps(receipt))
    return path
