"""管理后台端到端回归测试（/admin/* 与 Session 登录态）。"""
from __future__ import annotations

import unittest

from tests.fake_db import AsgiClient, make_test_app


class TestAdminPanel(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.pool, self.cfg = make_test_app()
        self.client = AsgiClient(self.app)

    def _login_admin(self) -> None:
        r = self.client.request(
            "POST",
            "/admin/login",
            form_body={"username": "admin", "password": "test-admin-pass"},
        )
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers.get("location"), "/admin")

    def test_require_login_redirect(self) -> None:
        for path in ("/admin", "/admin/keys", "/admin/users", "/admin/devices", "/admin/settings"):
            r = self.client.request("GET", path)
            self.assertEqual(r.status_code, 303, f"{path} 未登录应 303 跳转")
            self.assertEqual(r.headers.get("location"), "/admin/login")

        # 错密码登录留在登录页
        bad = self.client.request(
            "POST", "/admin/login", form_body={"username": "admin", "password": "wrong"}
        )
        self.assertEqual(bad.status_code, 200)
        self.assertIn("账号或密码错误", bad.text)

    def test_settings_live_update_reflects_in_app_config(self) -> None:
        self._login_admin()
        r = self.client.request(
            "POST",
            "/admin/settings",
            form_body={"kefu_qq": "88886666", "notice": "今晚维护 10 分钟"},
        )
        self.assertEqual(r.status_code, 303)

        cfg_resp = self.client.request("GET", "/api/v1/app/config").json()
        self.assertEqual(cfg_resp["kefu_qq"], "88886666")
        self.assertEqual(cfg_resp["notice"], "今晚维护 10 分钟")

    def test_generate_and_revoke_keys(self) -> None:
        self._login_admin()
        r = self.client.request(
            "POST",
            "/admin/keys/generate",
            form_body={"count": "3", "days": "30", "note": "测试批次"},
        )
        self.assertEqual(r.status_code, 303)
        self.assertEqual(len(self.pool.db.license_keys), 3)

        first_id = self.pool.db.license_keys[0]["id"]
        r_rev = self.client.request("POST", f"/admin/keys/{first_id}/revoke")
        self.assertEqual(r_rev.status_code, 303)
        self.assertTrue(self.pool.db.license_keys[0]["revoked"])

        # 概览与兑换码页可正常渲染
        self.assertEqual(self.client.request("GET", "/admin").status_code, 200)
        self.assertEqual(self.client.request("GET", "/admin/keys?status=revoked").status_code, 200)

    def test_user_unbind_phone_revoke_and_delete(self) -> None:
        # 注册两个用户，给 u1 绑定手机号
        u1 = self.client.request(
            "POST", "/api/v1/auth/register", json_body={"install_id": "adm-dev-1"}, client_ip="10.1.0.1"
        ).json()
        u2 = self.client.request(
            "POST", "/api/v1/auth/register", json_body={"install_id": "adm-dev-2"}, client_ip="10.1.0.2"
        ).json()
        self.client.request(
            "POST", "/api/v1/user/phone", json_body={"phone": "13800138001"}, headers={"X-Token": u1["token"]}
        )
        uid1 = self.pool.db.pcc_users[0]["id"]
        uid2 = self.pool.db.pcc_users[1]["id"]

        self._login_admin()
        # 1) 后台解绑 u1 手机号 -> u2 立刻可以绑定该手机号
        r_unbind = self.client.request("POST", f"/admin/users/{uid1}/unbind-phone")
        self.assertEqual(r_unbind.status_code, 303)
        ok_u2 = self.client.request(
            "POST", "/api/v1/user/phone", json_body={"phone": "13800138001"}, headers={"X-Token": u2["token"]}
        ).json()
        self.assertTrue(ok_u2["ok"])

        # 2) 后台作废 u2 -> u2 token 立即 401，且手机号被释放、登录提示 revoked
        r_rev = self.client.request("POST", f"/admin/users/{uid2}/revoke")
        self.assertEqual(r_rev.status_code, 303)
        self.assertEqual(
            self.client.request("POST", "/api/v1/record/start", headers={"X-Token": u2["token"]}).status_code,
            401,
        )
        lg_rev = self.client.request(
            "POST",
            "/api/v1/auth/login",
            json_body={"username": u2["username"], "password": u2["password"], "install_id": "adm-dev-2"},
        ).json()
        self.assertEqual(lg_rev["error_code"], "revoked")
        self.assertEqual(self.pool.db.pcc_users[1]["phone"], "")

        # 3) 后台删除 u2 -> 记录彻底移除
        r_del = self.client.request("POST", f"/admin/users/{uid2}/delete")
        self.assertEqual(r_del.status_code, 303)
        self.assertEqual(len(self.pool.db.pcc_users), 1)

        # 4) 用户页与设备页正常渲染
        self.assertEqual(self.client.request("GET", "/admin/users").status_code, 200)
        self.assertEqual(self.client.request("GET", "/admin/devices").status_code, 200)


if __name__ == "__main__":
    unittest.main()
