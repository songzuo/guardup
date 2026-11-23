#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checkout.py 自测：定价目录唯一真相源 + 主备双通道 URL 生成（不碰钱/凭证）。"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import checkout as C  # noqa: E402


class TestPricing(unittest.TestCase):
    def test_01_three_plans(self):
        plans = C.list_plans()
        self.assertEqual([p["name"] for p in plans], ["Seed", "Guard", "Fortress"])
        self.assertEqual(C.get_plan("guard").price_monthly, 79)
        with self.assertRaises(KeyError):
            C.get_plan("nope")

    def test_02_quote_with_onboarding(self):
        q = C.quote("guard", with_onboarding=True)
        self.assertEqual(q["monthly_usd"], 79)
        self.assertEqual(q["first_charge_usd"], 79 + 99)
        self.assertEqual(C.quote("seed")["onboarding_usd"], 0)


class TestCheckoutUrl(unittest.TestCase):
    def test_03_placeholder_url_without_credentials(self):
        # 未配置环境变量 → 占位 id，流程可跑通，不泄露真实凭证
        os.environ.pop("GUARDUP_CREEM_PRODUCT_GUARD", None)
        url = C.checkout_url("creem", "guard")
        self.assertIn("creem.io/checkout", url)
        self.assertIn("PLACEHOLDER", url)

    def test_04_env_product_id(self):
        os.environ["GUARDUP_CREEM_PRODUCT_GUARD"] = "prod_real123"
        try:
            self.assertIn("product_id=prod_real123", C.checkout_url("creem", "guard"))
        finally:
            os.environ.pop("GUARDUP_CREEM_PRODUCT_GUARD", None)

    def test_05_dual_checkout_both_channels(self):
        d = C.dual_checkout("fortress", email="buyer@example.com")
        self.assertEqual(d["primary"]["provider"], "creem")
        self.assertEqual(d["fallback"]["provider"], "dodo")
        self.assertIn("checkout.dodopayments.com", d["fallback"]["url"])
        self.assertIn("customer_email=buyer@example.com", d["primary"]["url"])

    def test_06_unknown_provider(self):
        with self.assertRaises(KeyError):
            C.checkout_url("paypal", "guard")


if __name__ == "__main__":
    unittest.main(verbosity=2)
