from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

password_hasher = PasswordHasher()

def hash_password(password: str) -> str:
    """哈希密码，忽略最小长度检验（CLI 重置时允许短密码）"""
    return password_hasher.hash(password)

def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False

def validate_password(password: str) -> None:
    """UI 设置密码时用的强度校验（至少 6 字符）"""
    if len(password) < 6:
        raise ValueError("密码至少需要 6 个字符")
    if len(password) > 256:
        raise ValueError("密码过长")
