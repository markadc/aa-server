"""APK 客户端全链路 API 回归测试（/api/v1/*）。"""
from __future__ import annotations

import unittest

from app import auth
from tests.fake_db import AsgiClient, make_test_app


class TestApiEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.pool, self.cfg = make_test_app()
        self.client = AsgiClient(self.app)

    def test_health_ping_and_config(self) -> None:
        r = self.client.request("GET", "/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

        r = self.client.request("GET", "/api/v1/ping")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "pcc")
        self.assertIn("server_time", r.json())

        # 未在后台保存过时，回落 config.toml 默认值
        r = self.client.request("GET", "/api/v1/app/config")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["kefu_qq"], "123456789")
        self.assertEqual(r.json()["notice"], "默认公告")

    def test_device_register_upsert_preserves_non_empty(self) -> None:
        r1 = self.client.request(
            "POST",
            "/api/v1/device/register",
            json_body={
                "install_id": "inst-0001",
                "android_id": "aid-1",
                "model": "Pixel 8",
                "os_version": "15",
                "app_version": "pcc_v3.1.1",
            },
        )
        self.assertEqual(r1.status_code, 200)
        self.assertTrue(r1.json()["ok"])

        # 第二次上报空 model，不应覆盖已有 model
        r2 = self.client.request(
            "POST",
            "/api/v1/device/register",
            json_body={"install_id": "inst-0001", "model": "", "app_version": "pcc_v3.1.2"},
        )
        self.assertEqual(r2.status_code, 200)
        dev = self.pool.db.devices["inst-0001"]
        self.assertEqual(dev["model"], "Pixel 8")
        self.assertEqual(dev["app_version"], "pcc_v3.1.2")

    def test_register_and_rate_limit(self) -> None:
        r1 = self.client.request(
            "POST", "/api/v1/auth/register", json_body={"install_id": "dev-aaaaaaaa"}, client_ip="10.0.0.10"
        )
        self.assertEqual(r1.status_code, 200)
        d1 = r1.json()
        self.assertTrue(d1["ok"])
        self.assertFalse(d1["reused"])
        self.assertEqual(d1["license_expires_at_ms"], 0)
        self.assertEqual(d1["phone"], "")

        # 同设备立即再注册 -> 被限流
        r2 = self.client.request(
            "POST", "/api/v1/auth/register", json_body={"install_id": "dev-aaaaaaaa"}, client_ip="10.0.0.11"
        )
        self.assertFalse(r2.json()["ok"])
        self.assertEqual(r2.json()["error_code"], "rate_limited")

        # 换设备但同 IP -> 被 IP 限流
        r3 = self.client.request(
            "POST", "/api/v1/auth/register", json_body={"install_id": "dev-bbbbbbbb"}, client_ip="10.0.0.10"
        )
        self.assertFalse(r3.json()["ok"])
        self.assertEqual(r3.json()["error_code"], "rate_limited")

        # 换设备 + 换 IP -> 放行新建不同账号
        r4 = self.client.request(
            "POST", "/api/v1/auth/register", json_body={"install_id": "dev-bbbbbbbb"}, client_ip="10.0.0.12"
        )
        self.assertTrue(r4.json()["ok"])
        self.assertNotEqual(r4.json()["username"], d1["username"])

    def test_login_kicks_old_token_and_handles_errors(self) -> None:
        reg = self.client.request(
            "POST", "/api/v1/auth/register", json_body={"install_id": "dev-11111111"}, client_ip="10.0.1.1"
        ).json()
        username, password, old_token = reg["username"], reg["password"], reg["token"]

        # 旧 token 可调 /record/start
        ok1 = self.client.request("POST", "/api/v1/record/start", headers={"X-Token": old_token})
        self.assertEqual(ok1.status_code, 200)

        # 错密码 / 不存在
        bad_pwd = self.client.request(
            "POST",
            "/api/v1/auth/login",
            json_body={"username": username, "password": "wrong", "install_id": "dev-22222222"},
        ).json()
        self.assertEqual(bad_pwd["error_code"], "bad_password")

        not_found = self.client.request(
            "POST",
            "/api/v1/auth/login",
            json_body={"username": "user_missing", "password": "pwd0000", "install_id": "dev-22222222"},
        ).json()
        self.assertEqual(not_found["error_code"], "not_found")

        # 新设备登录成功 -> 轮换 token，旧 token 立即 401（单设备互踢）
        login = self.client.request(
            "POST",
            "/api/v1/auth/login",
            json_body={"username": username, "password": password, "install_id": "dev-22222222"},
        ).json()
        self.assertTrue(login["ok"])
        new_token = login["token"]
        self.assertNotEqual(new_token, old_token)

        kicked = self.client.request("POST", "/api/v1/record/start", headers={"X-Token": old_token})
        self.assertEqual(kicked.status_code, 401)
        self.assertEqual(kicked.json()["error_code"], "unauthorized")

        ok2 = self.client.request("POST", "/api/v1/record/start", headers={"X-Token": new_token})
        self.assertEqual(ok2.status_code, 200)
        self.assertEqual(ok2.json()["username"], username)

    def test_quick_login_legacy_reuse(self) -> None:
        r1 = self.client.request(
            "POST", "/api/v1/auth/quick-login", json_body={"install_id": "legacy-dev-1"}, client_ip="10.0.2.1"
        ).json()
        self.assertTrue(r1["ok"])
        self.assertFalse(r1["reused"])

        # 同设备再次 quick-login 复用原账号（不触发限流）并轮换 token
        r2 = self.client.request(
            "POST", "/api/v1/auth/quick-login", json_body={"install_id": "legacy-dev-1"}, client_ip="10.0.2.1"
        ).json()
        self.assertTrue(r2["ok"])
        self.assertTrue(r2["reused"])
        self.assertEqual(r2["username"], r1["username"])
        self.assertNotEqual(r2["token"], r1["token"])

    def test_redeem_account_and_offline_normalization_and_legacy(self) -> None:
        # 准备 3 个码：普通后台码、存量 16 位离线码、已作废码
        self.pool.db.add_key("PCC-ABCD-EFGH-2345", days=30)
        self.pool.db.add_key("PCC-01E0-0001-ABCD-EF12", days=15)
        self.pool.db.add_key("PCC-REVO-KED0-9999", days=7, revoked=True)

        reg = self.client.request(
            "POST", "/api/v1/auth/register", json_body={"install_id": "dev-redeem-1"}, client_ip="10.0.3.1"
        ).json()
        token = reg["token"]

        # 1) 带 X-Token 核销后台码 -> 绑定到账号
        r1 = self.client.request(
            "POST",
            "/api/v1/license/redeem",
            json_body={"code": "pcc-abcd-efgh-2345", "install_id": "dev-redeem-1"},
            headers={"X-Token": token},
        ).json()
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["days"], 30)
        self.assertEqual(r1["username"], reg["username"])
        exp1 = r1["expires_at_ms"]
        self.assertGreater(exp1, 0)

        # 2) 重复核销同一个码 -> used
        r_dup = self.client.request(
            "POST",
            "/api/v1/license/redeem",
            json_body={"code": "PCC-ABCD-EFGH-2345", "install_id": "dev-redeem-1"},
            headers={"X-Token": token},
        ).json()
        self.assertFalse(r_dup["ok"])
        self.assertEqual(r_dup["error_code"], "used")

        # 3) 用无连字符无前缀的 16 位 hex 核销存量离线码 -> 自动归一化匹配，且天数累加
        r2 = self.client.request(
            "POST",
            "/api/v1/license/redeem",
            json_body={"code": "01e00001abcdef12", "install_id": "dev-redeem-1"},
            headers={"X-Token": token},
        ).json()
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["days"], 15)
        self.assertGreater(r2["expires_at_ms"], exp1)

        # 4) 作废码 / 不存在码 / 无效 token
        r_rev = self.client.request(
            "POST",
            "/api/v1/license/redeem",
            json_body={"code": "PCC-REVO-KED0-9999", "install_id": "dev-redeem-1"},
            headers={"X-Token": token},
        ).json()
        self.assertEqual(r_rev["error_code"], "revoked")

        r_nf = self.client.request(
            "POST",
            "/api/v1/license/redeem",
            json_body={"code": "PCC-NOPE-NOPE-NOPE", "install_id": "dev-redeem-1"},
            headers={"X-Token": token},
        ).json()
        self.assertEqual(r_nf["error_code"], "not_found")

        r_bad_tok = self.client.request(
            "POST",
            "/api/v1/license/redeem",
            json_body={"code": "PCC-NOPE-NOPE-NOPE", "install_id": "dev-redeem-1"},
            headers={"X-Token": "invalid-token"},
        )
        self.assertEqual(r_bad_tok.status_code, 401)

        # 5) 账号 /record/start 与 /license/status 同步返回最新授权
        st = self.client.request("POST", "/api/v1/record/start", headers={"X-Token": token}).json()
        self.assertTrue(st["license"]["active"])
        self.assertEqual(st["license"]["expires_at_ms"], r2["expires_at_ms"])

        ls = self.client.request(
            "GET", "/api/v1/license/status?install_id=dev-redeem-1", headers={"X-Token": token}
        ).json()
        self.assertTrue(ls["active"])
        self.assertEqual(ls["username"], reg["username"])

        # 6) 兼容老包（不带 X-Token）：核销写 devices 表
        self.pool.db.add_key("PCC-LEGA-CY00-0001", days=7)
        r_leg = self.client.request(
            "POST",
            "/api/v1/license/redeem",
            json_body={"code": "PCC-LEGA-CY00-0001", "install_id": "old-apk-dev-1"},
        ).json()
        self.assertTrue(r_leg["ok"])
        self.assertEqual(r_leg["username"], "")
        ls_leg = self.client.request("GET", "/api/v1/license/status?install_id=old-apk-dev-1").json()
        self.assertTrue(ls_leg["active"])

    def test_bind_and_modify_phone_unique_1_to_1(self) -> None:
        # 未登录禁止绑定
        unauth = self.client.request("POST", "/api/v1/user/phone", json_body={"phone": "13800138000"})
        self.assertEqual(unauth.status_code, 401)

        u1 = self.client.request(
            "POST", "/api/v1/auth/register", json_body={"install_id": "dev-phone-1"}, client_ip="10.0.4.1"
        ).json()
        u2 = self.client.request(
            "POST", "/api/v1/auth/register", json_body={"install_id": "dev-phone-2"}, client_ip="10.0.4.2"
        ).json()

        # 非法手机号被拒绝
        bad = self.client.request(
            "POST", "/api/v1/user/phone", json_body={"phone": "12345"}, headers={"X-Token": u1["token"]}
        ).json()
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error_code"], "invalid_phone")

        # u1 正常绑定（支持自动去横杠/空格）
        ok1 = self.client.request(
            "POST", "/api/v1/user/phone", json_body={"phone": "138-0013-8000"}, headers={"X-Token": u1["token"]}
        ).json()
        self.assertTrue(ok1["ok"])
        self.assertEqual(ok1["phone"], "13800138000")

        # u2 尝试抢绑同一手机号 -> phone_taken
        dup = self.client.request(
            "POST", "/api/v1/user/phone", json_body={"phone": "13800138000"}, headers={"X-Token": u2["token"]}
        ).json()
        self.assertFalse(dup["ok"])
        self.assertEqual(dup["error_code"], "phone_taken")

        # u1 自助换绑新手机号 -> 成功，原手机号释放给 u2
        mod = self.client.request(
            "POST", "/api/v1/user/phone", json_body={"phone": "13900139000"}, headers={"X-Token": u1["token"]}
        ).json()
        self.assertTrue(mod["ok"])
        self.assertEqual(mod["phone"], "13900139000")

        ok2 = self.client.request(
            "POST", "/api/v1/user/phone", json_body={"phone": "13800138000"}, headers={"X-Token": u2["token"]}
        ).json()
        self.assertTrue(ok2["ok"])

        # /record/start 与 /auth/login 均下发最新手机号
        st = self.client.request("POST", "/api/v1/record/start", headers={"X-Token": u1["token"]}).json()
        self.assertEqual(st["phone"], "13900139000")

        auth.reset_rate_limit()
        lg = self.client.request(
            "POST",
            "/api/v1/auth/login",
            json_body={"username": u1["username"], "password": u1["password"], "install_id": "dev-phone-1"},
        ).json()
        self.assertEqual(lg["phone"], "13900139000")

    def test_feedback_requires_login_and_records_context_and_rate_limits(self) -> None:
        # 未登录禁止提交
        unauth = self.client.request("POST", "/api/v1/feedback", json_body={"content": "悬浮窗位置不错"})
        self.assertEqual(unauth.status_code, 401)

        u = self.client.request(
            "POST", "/api/v1/auth/register", json_body={"install_id": "dev-fb-1"}, client_ip="10.0.5.1"
        ).json()
        self.client.request(
            "POST", "/api/v1/user/phone", json_body={"phone": "13700137000"}, headers={"X-Token": u["token"]}
        )

        # 内容过短被拒绝（不触发限流）
        short = self.client.request(
            "POST",
            "/api/v1/feedback",
            json_body={"content": " 1 "},
            headers={"X-Token": u["token"]},
            client_ip="10.0.5.1",
        ).json()
        self.assertFalse(short["ok"])
        self.assertEqual(short["error_code"], "content_too_short")

        # 正常提交：自动记录账号、已绑手机号、联系方式、机型与版本
        ok = self.client.request(
            "POST",
            "/api/v1/feedback",
            json_body={
                "content": "大掼蛋发牌时偶尔漏识别一张",
                "contact": "wx:poker88",
                "install_id": "dev-fb-1",
                "app_version": "pcc_v3.1.2",
                "model": "Xiaomi 14",
                "os_version": "15",
            },
            headers={"X-Token": u["token"]},
            client_ip="10.0.5.1",
        )
        self.assertEqual(ok.status_code, 200)
        body = ok.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["id"], 1)
        self.assertEqual(len(self.pool.db.pcc_feedbacks), 1)
        saved = self.pool.db.pcc_feedbacks[0]
        self.assertEqual(saved["username"], u["username"])
        self.assertEqual(saved["phone"], "13700137000")
        self.assertEqual(saved["contact"], "wx:poker88")
        self.assertEqual(saved["model"], "Xiaomi 14")
        self.assertEqual(saved["status"], "open")

        # 同账号 60 秒内重复提交 -> 限流
        limited = self.client.request(
            "POST",
            "/api/v1/feedback",
            json_body={"content": "再提交一条"},
            headers={"X-Token": u["token"]},
            client_ip="10.0.5.1",
        ).json()
        self.assertFalse(limited["ok"])
        self.assertEqual(limited["error_code"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
