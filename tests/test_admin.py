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
        for path in ("/admin", "/admin/keys", "/admin/users", "/admin/devices", "/admin/feedbacks", "/admin/crashes", "/admin/settings"):
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

    def test_feedbacks_list_resolve_reopen_and_delete(self) -> None:
        u = self.client.request(
            "POST", "/api/v1/auth/register", json_body={"install_id": "adm-fb-1"}, client_ip="10.2.0.1"
        ).json()
        self.client.request(
            "POST",
            "/api/v1/feedback",
            json_body={"content": "希望能增加音效提醒", "contact": "qq12345", "model": "Pixel 9"},
            headers={"X-Token": u["token"]},
            client_ip="10.2.0.1",
        )
        fid = self.pool.db.pcc_feedbacks[0]["id"]

        self._login_admin()
        page = self.client.request("GET", "/admin/feedbacks?status=open&q=音效")
        self.assertEqual(page.status_code, 200)
        self.assertIn("希望能增加音效提醒", page.text)
        self.assertIn("qq12345", page.text)

        # 标为已处理
        r1 = self.client.request("POST", f"/admin/feedbacks/{fid}/status", form_body={"status": "resolved"})
        self.assertEqual(r1.status_code, 303)
        self.assertEqual(self.pool.db.pcc_feedbacks[0]["status"], "resolved")

        # 重开为待处理
        r2 = self.client.request("POST", f"/admin/feedbacks/{fid}/status", form_body={"status": "open"})
        self.assertEqual(r2.status_code, 303)
        self.assertEqual(self.pool.db.pcc_feedbacks[0]["status"], "open")

        # 管理员回复反馈（自动标为 resolved）
        rep = self.client.request("POST", f"/admin/feedbacks/{fid}/reply", form_body={"reply": "收到，明天修"})
        self.assertEqual(rep.status_code, 303)
        self.assertEqual(self.pool.db.pcc_feedbacks[0]["reply"], "收到，明天修")
        self.assertEqual(self.pool.db.pcc_feedbacks[0]["status"], "resolved")

        # 删除反馈
        r3 = self.client.request("POST", f"/admin/feedbacks/{fid}/delete")
        self.assertEqual(r3.status_code, 303)
        self.assertEqual(len(self.pool.db.pcc_feedbacks), 0)

    def test_crashes_page_and_settings_json_validation(self) -> None:
        self._login_admin()
        # 写入一条崩溃日志并查看后台
        self.client.request(
            "POST",
            "/api/v1/crash",
            json_body={
                "install_id": "dev-adm-crash",
                "model": "Mate 60",
                "message": "IllegalStateException",
                "stacktrace": "trace line 1",
            },
            client_ip="10.0.9.9",
        )
        page = self.client.request("GET", "/admin/crashes?q=Mate")
        self.assertEqual(page.status_code, 200)
        self.assertIn("IllegalStateException", page.text)

        cid = self.pool.db.pcc_crash_logs[0]["id"]
        d1 = self.client.request("POST", f"/admin/crashes/{cid}/delete")
        self.assertEqual(d1.status_code, 303)
        self.assertEqual(len(self.pool.db.pcc_crash_logs), 0)

        # settings JSON 非法格式拦截
        bad = self.client.request(
            "POST",
            "/admin/settings",
            form_body={
                "kefu_qq": "123",
                "notice": "",
                "api_base_url": "ftp://invalid",
                "seed_version": "2",
                "seed_patch_json": "",
                "games_json": "",
            },
        )
        self.assertEqual(bad.status_code, 200)
        self.assertIn("必须以 http:// 或 https:// 开头", bad.text)

        bad_json = self.client.request(
            "POST",
            "/admin/settings",
            form_body={
                "kefu_qq": "123",
                "notice": "",
                "api_base_url": "http://1.2.3.4:8800/",
                "seed_version": "2",
                "seed_patch_json": "[1,2,3]",
                "games_json": "",
            },
        )
        self.assertEqual(bad_json.status_code, 200)
        self.assertIn("必须是 JSON 对象", bad_json.text)

        # 合法配置保存成功且自动去掉尾部斜杠
        good = self.client.request(
            "POST",
            "/admin/settings",
            form_body={
                "kefu_qq": "123",
                "notice": "新公告",
                "api_base_url": "http://1.2.3.4:8800/",
                "seed_version": "3",
                "seed_patch_json": '{"k":"v"}',
                "games_json": '[{"name":"测试","platform":"测试","mode":0}]',
            },
        )
        self.assertEqual(good.status_code, 303)
        self.assertEqual(self.pool.db.app_settings["api_base_url"]["value"], "http://1.2.3.4:8800")
        self.assertEqual(self.pool.db.app_settings["seed_version"]["value"], "3")


if __name__ == "__main__":
    unittest.main()
