"""V2 集成补充：观察项移除入口（Goal §3.1 / AC-01）的 GUI 锚点。

真实场景（orx-analytics）发现的缺口：服务 ⋯ 菜单对 observe_only 一律显示
「移除登记…」但 askRemoveService 直接 return（点击无反应）。修复后观察项走
专用模态：只解除登记（keep-external 语义），绝不停止外部引擎。
"""

from __future__ import annotations

import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "localservices" / "web" / "dashboard.html").read_text(encoding="utf-8")


class ObserveRemoveAnchorTest(unittest.TestCase):
    def test_observe_remove_dialog_exists(self):
        self.assertIn("askRemoveObserve", HTML)
        self.assertIn("if (p.observe_only) return askRemoveObserve(sid, p);", HTML)
        # 观察项模态：声明外部引擎照常运行 + 不提供停止处置
        self.assertIn("照常运行", HTML)
        self.assertIn("只解除本工具的观察登记", HTML)

    def test_service_menu_has_remove(self):
        self.assertIn("function serviceMenuItems", HTML)
        self.assertIn('label: tr("移除登记…", "Remove registration…"), danger: true', HTML)


if __name__ == "__main__":
    unittest.main()
