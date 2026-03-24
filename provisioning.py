# provisioning.py
from __future__ import annotations
import os, json, uuid, tempfile
from typing import Dict, Tuple, Optional

# 기본 경로는 기존 스크립트와 동일하게 유지
DEFAULT_TOKEN_FILE = "/etc/mosquitto/provisioning_tokens.json"
ENV_TOKEN_FILE = "PROVISIONING_TOKENS"  # 필요시 경로 오버라이드

def _token_path(path: Optional[str] = None) -> str:
    return path or os.getenv(ENV_TOKEN_FILE, DEFAULT_TOKEN_FILE)

def load_tokens(path: Optional[str] = None) -> Dict[str, str]:
    p = _token_path(path)
    if not os.path.exists(p):
        return {}
    with open(p, "r") as f:
        data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"토큰 파일 포맷 오류: {p}")
        # 값은 문자열(토큰)만 허용
        for k, v in list(data.items()):
            if not isinstance(v, str):
                raise ValueError(f"잘못된 토큰 값: {k} -> {type(v)}")
        return data

def _atomic_write_json(obj: dict, path: str) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=d, delete=False) as tmp:
        json.dump(obj, tmp, indent=4, ensure_ascii=False)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    os.replace(tmp_name, path)  # 원자적 교체

def save_tokens(tokens: Dict[str, str], path: Optional[str] = None) -> None:
    _atomic_write_json(tokens, _token_path(path))

def get_token(gateway_id: str, path: Optional[str] = None) -> Optional[str]:
    tokens = load_tokens(path)
    return tokens.get(gateway_id)

def verify_token(gateway_id: str, token: str, path: Optional[str] = None) -> bool:
    stored = get_token(gateway_id, path)
    return stored is not None and stored == token

def generate_token(gateway_id: str, *, overwrite: bool = False,
                   path: Optional[str] = None) -> Tuple[str, bool]:
    """
    반환: (token, created_new)
      - created_new=True: 새로 발급
      - created_new=False: 기존 토큰 유지(또는 overwrite=True면 재발급)
    """
    if not gateway_id:
        raise ValueError("gateway_id가 비었습니다.")
    tokens = load_tokens(path)
    if gateway_id in tokens and not overwrite:
        return tokens[gateway_id], False  # 기존 유지
    new_token = str(uuid.uuid4())
    tokens[gateway_id] = new_token
    save_tokens(tokens, path)
    return new_token, True

def revoke_token(gateway_id: str, path: Optional[str] = None) -> bool:
    tokens = load_tokens(path)
    if gateway_id in tokens:
        del tokens[gateway_id]
        save_tokens(tokens, path)
        return True
    return False

def list_gateways(path: Optional[str] = None) -> Dict[str, str]:
    return load_tokens(path)
