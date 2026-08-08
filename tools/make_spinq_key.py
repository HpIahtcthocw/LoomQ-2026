#!/usr/bin/env python3
"""为量旋云生成一对 RSA 密钥：私钥留本地，公钥贴到 cloud.spinq.cn。

为什么不直接用 ssh-keygen：量旋 SDK 在 `SpinQCloudBackend.__init__` 里用
`Crypto.PublicKey.RSA.importKey()` 读私钥，它只认 PEM（`-----BEGIN RSA PRIVATE KEY-----`）。
而 OpenSSH 7.8 以后 `ssh-keygen` 默认输出的是自有格式
（`-----BEGIN OPENSSH PRIVATE KEY-----`），importKey 会直接抛异常。
用 ssh-keygen 的话必须显式加 `-m PEM`。这里改用 PyCryptodome 直接生成，格式完全可控，
也不依赖机器上装没装 OpenSSH。

私钥默认写到用户主目录下的 ~/.spinq/，**刻意放在仓库之外**，避免任何形式的误提交。

用法：
    python tools/make_spinq_key.py                 # 生成（已存在则不覆盖）
    python tools/make_spinq_key.py --show-public    # 只重新打印公钥
    python tools/make_spinq_key.py --force          # 强制重新生成
"""

from __future__ import annotations

import argparse
import os
import sys

try:
    from Crypto.Hash import SHA256
    from Crypto.PublicKey import RSA
    from Crypto.Signature import pkcs1_15
except ImportError:
    print("缺少 pycryptodome。它是 spinqit 的依赖，请在装了 spinqit 的 3.10 环境里跑：")
    print("  .venv\\Scripts\\python.exe tools/make_spinq_key.py")
    sys.exit(2)

KEY_DIR = os.path.join(os.path.expanduser("~"), ".spinq")
PRIVATE_PATH = os.path.join(KEY_DIR, "spinq_cloud_rsa")
PUBLIC_PATH = PRIVATE_PATH + ".pub"


def generate(force: bool) -> None:
    if os.path.exists(PRIVATE_PATH) and not force:
        print("私钥已存在，不覆盖：%s" % PRIVATE_PATH)
        print("要重新生成请加 --force（注意：重新生成后网站上的公钥也必须换掉）")
        return

    os.makedirs(KEY_DIR, exist_ok=True)
    key = RSA.generate(2048)

    with open(PRIVATE_PATH, "wb") as handle:
        handle.write(key.export_key(format="PEM"))
    try:
        os.chmod(PRIVATE_PATH, 0o600)
    except OSError:
        pass  # Windows 上 chmod 基本无效，忽略

    with open(PUBLIC_PATH, "wb") as handle:
        handle.write(key.publickey().export_key(format="OpenSSH"))

    print("已生成：")
    print("  私钥 %s   ← 留在本机，不要给任何人，也不要放进仓库" % PRIVATE_PATH)
    print("  公钥 %s   ← 贴到 cloud.spinq.cn 的账号设置里" % PUBLIC_PATH)


def verify_roundtrip(username: str) -> bool:
    """按 spinqit 的签名路径实跑一遍，确认这把私钥能被它读进去并正确签名。

    spinqit 的做法是：用私钥对用户名做 PKCS#1 v1.5 + SHA256 签名，base64 后当凭证。
    这里用公钥验一遍签名，能过就说明格式与算法都对得上，不用等连服务器才发现问题。
    """
    with open(PRIVATE_PATH) as handle:
        private = RSA.importKey(handle.read())
    digest = SHA256.new(username.encode("utf-8"))
    signature = pkcs1_15.new(private).sign(digest)
    try:
        pkcs1_15.new(private.publickey()).verify(SHA256.new(username.encode("utf-8")), signature)
        return True
    except (ValueError, TypeError):
        return False


def show_public() -> None:
    if not os.path.exists(PUBLIC_PATH):
        print("还没有公钥，先跑一次不带参数的本脚本")
        return
    with open(PUBLIC_PATH) as handle:
        content = handle.read().strip()
    print()
    print("=" * 72)
    print("把下面这一整行（含开头的 ssh-rsa）复制，粘贴到 cloud.spinq.cn 账号设置的 SSH 公钥处：")
    print("=" * 72)
    print(content)
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成量旋云 RSA 密钥对")
    parser.add_argument("--force", action="store_true", help="覆盖已有密钥")
    parser.add_argument("--show-public", action="store_true", help="只打印公钥")
    parser.add_argument("--username", default="20260808", help="用于验证签名链路的用户名")
    args = parser.parse_args()

    if not args.show_public:
        generate(args.force)
        if os.path.exists(PRIVATE_PATH):
            ok = verify_roundtrip(args.username)
            print()
            print("签名链路自检（模拟 spinqit 的登录签名）：%s" % ("通过" if ok else "失败"))
            if not ok:
                return 1

    show_public()
    return 0


if __name__ == "__main__":
    sys.exit(main())
