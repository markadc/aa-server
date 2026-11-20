"""核心单元测试：密码哈希、限流逻辑、兑换码格式归一化、离线码解析。"""
from __future__ import annotations

import hashlib
import re
import unittest

from app import api, auth, security
from scripts.import_offline_keys import SALT, parse_offline_key


class TestSecurity(unittest.TestCase):
    def test_hash_and_verify(self) -> None:
        h = security.hash_password("s3cr3t!")
        self.assertTrue(h.startswith("pbkdf2_sha256$"))
        self.assertTrue(security.verify_password("s3cr3t!", h))
        self.assertFalse(security.verify_password("wrong", h))
        self.assertFalse(security.verify_password("s3cr3t!", "bad-format"))
        self.assertFalse(security.verify_password("s3cr3t!", "md5$1$aa$bb"))


class TestAuthHelpers(unittest.TestCase):
    def setUp(self) -> None:
        auth.reset_rate_limit()

    def test_generators(self) -> None:
        self.assertRegex(auth.gen_username(), r"^user\d{4}$")
        self.assertRegex(auth.gen_password(), r"^pwd\d{4}$")
        t1, t2 = auth.gen_token(), auth.gen_token()
        self.assertNotEqual(t1, t2)
        self.assertGreaterEqual(len(t1), 32)

    def test_register_rate_limit_device_and_ip(self) -> None:
        self.assertIsNone(auth.rate_limit("dev-1", interval=60, hourly_max=20))
        self.assertIsNone(auth.rate_limit_ip("1.2.3.4", interval=10, hourly_max=30))

        auth.mark_success("dev-1", "1.2.3.4")
        self.assertIsNotNone(auth.rate_limit("dev-1", interval=60, hourly_max=20))
        self.assertIsNotNone(auth.rate_limit_ip("1.2.3.4", interval=10, hourly_max=30))
        # 不同设备 + 不同 IP 不受影响
        self.assertIsNone(auth.rate_limit("dev-2", interval=60, hourly_max=20))
        self.assertIsNone(auth.rate_limit_ip("5.6.7.8", interval=10, hourly_max=30))

    def test_login_fail_rate_limit(self) -> None:
        ip = "9.9.9.9"
        for _ in range(auth.LOGIN_FAIL_MAX - 1):
            auth.mark_login_fail(ip)
            self.assertIsNone(auth.check_login_rate_limit(ip))
        auth.mark_login_fail(ip)
        self.assertIsNotNone(auth.check_login_rate_limit(ip))


class TestCodeHelpers(unittest.TestCase):
    def test_gen_code_format_no_ambiguous_chars(self) -> None:
        for _ in range(50):
            c = api.gen_code()
            self.assertRegex(c, r"^PCC-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
            body = c[4:].replace("-", "")
            for ch in ("0", "O", "1", "I"):
                self.assertNotIn(ch, body)

    def test_code_candidates_normalization(self) -> None:
        # 后台 12 位码：只保留去空白大写
        self.assertEqual(api._code_candidates(" pcc-ab23-cd45-ef67 "), ["PCC-AB23-CD45-EF67"])

        # 16 位 hex 离线码：无前缀/无连字符 -> 自动附带 PCC-XXXX-... 标准形态
        cands = api._code_candidates("01e00001abcdef12")
        self.assertEqual(cands[0], "01E00001ABCDEF12")
        self.assertIn("PCC-01E0-0001-ABCD-EF12", cands)

        # 已经是标准形态时不重复
        pretty = "PCC-01E0-0001-ABCD-EF12"
        self.assertEqual(api._code_candidates(pretty), [pretty])

    def test_parse_offline_key(self) -> None:
        data8 = f"{30:03X}A1B2C"
        sig8 = hashlib.md5((SALT + data8).encode("utf-8")).hexdigest().upper()[:8]
        raw = data8 + sig8

        parsed = parse_offline_key(raw)
        self.assertIsNotNone(parsed)
        code, days = parsed
        self.assertEqual(days, 30)
        self.assertTrue(re.match(r"^PCC-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}$", code))

        # 带前缀与连字符也能解析
        self.assertEqual(parse_offline_key(code), (code, 30))

        # 篡改签名 / 长度错 / 0 天 必须拒绝
        self.assertIsNone(parse_offline_key(data8 + "00000000"))
        self.assertIsNone(parse_offline_key("PCC-ABCD-EFGH"))
        zero_data = "000A1B2C"
        zero_sig = hashlib.md5((SALT + zero_data).encode("utf-8")).hexdigest().upper()[:8]
        self.assertIsNone(parse_offline_key(zero_data + zero_sig))


if __name__ == "__main__":
    unittest.main()
