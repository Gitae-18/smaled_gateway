from __future__ import annotations
import os, time, tempfile, threading
from typing import Any, Dict, Optional
import pickle

class NodeConfigStore:
    """
    노드별 설정 전용 스토어.

    - nodes_store.bin 과 같은 디렉터리에 node_config.bin 파일을 생성/유지
    - uid/mid 조합으로 노드 식별
    - 한 노드에 대해 아래 같은 설정들을 저장:
        * light_schedule: 조명 on/off 스케줄 및 기본 상태
        * snapshot_period_ms: 스냅샷 주기 (ms)
        * status_period_ms: 상태 보고 주기 (선택)
        * backup: 백업 배터리 관련 설정
        * ai_policy: AI 임계값/알람 정책
        * 기타 필요한 값들은 extra(dict)에 넣어서 확장 가능
    """

    def __init__(self, path: Optional[str] = None):
        # 기본 경로: nodes_store.bin 과 같은 디렉터리의 node_config.bin
        if path is None:
            # ⚠️ 실제 NodeStore 기본 경로가 다르면 여기 문자열만 맞춰주면 됨
            default_nodes = "nodes_store.bin"
            base_dir = os.path.dirname(default_nodes) or "."
            path = os.path.join(base_dir, "node_config.bin")

        self.path = path
        self._lock = threading.Lock()
        self._by_key: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ---------------------------------------------------------
    # 내부 유틸
    # ---------------------------------------------------------
    def _mk_key(self, uid: Optional[str], mid: Optional[int]) -> str:
        uid_part = f"uid:{uid}" if uid is not None else ""
        mid_part = f"mid:{mid}" if mid is not None else ""
        if uid_part and mid_part:
            return f"{uid_part}|{mid_part}"
        return uid_part or mid_part or "unknown"

    def _load(self) -> None:
        """파일에서 설정 로드."""
        if not os.path.exists(self.path):
            return

        try:
            with open(self.path, "rb") as f:
                obj = pickle.load(f)

            # {"version":1, "saved_at":..., "nodes":{...}} 형태 기대
            if isinstance(obj, dict) and "nodes" in obj:
                nodes = obj["nodes"]
                if isinstance(nodes, dict):
                    self._by_key = nodes
                else:
                    print("[NodeConfigStore] invalid nodes type in file, ignoring")
            elif isinstance(obj, dict):
                self._by_key = obj
            else:
                print("[NodeConfigStore] unexpected file content, ignoring")
        except Exception as e:
            print(f"[NodeConfigStore] load error: {e!r}")
            self._by_key = {}

    def _atomic_save(self) -> None:
        """현재 설정을 바이너리로 원자적으로 저장."""
        tmp_dir = os.path.dirname(self.path) or "."
        os.makedirs(tmp_dir, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(prefix=".node_config_tmp", dir=tmp_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                payload = {
                    "version": 1,
                    "saved_at": time.time(),
                    "nodes": self._by_key,
                }
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except Exception as e:
            print(f"[NodeConfigStore] save error: {e!r}")
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # ---------------------------------------------------------
    # 퍼블릭 API
    # ---------------------------------------------------------
    def upsert(
        self,
        uid: Optional[str],
        mid: Optional[int],
        *,
        light_schedule: Optional[Dict[str, Any]] = None,
        snapshot_period_ms: Optional[int] = None,
        status_period_ms: Optional[int] = None,
        backup: Optional[Dict[str, Any]] = None,
        ai_policy: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        노드 설정을 생성/업데이트한다.
        - None 으로 들어온 필드는 기존 값 유지

        light_schedule 예시:
            {
                "default": "off",         # "on" / "off" / "restore_last"
                "on_times": ["18:00"],
                "off_times": ["23:00"],
            }

        backup 예시:
            {
                "low_voltage": 11.0,
                "critical_voltage": 10.5,
                "max_run_minutes": 60,
                "behavior": "turn_off_light",  # 또는 "limit_only", "shutdown_node"
            }

        ai_policy 예시:
            {
                "enabled": True,
                "model_version": 3,
                "ultrasonic_rms_hi": 0.8,
                "ultrasonic_rms_lo": 0.05,
                "voltage_hi": 250.0,
                "voltage_lo": 180.0,
                "current_hi": 2.5,
                "current_lo": 0.1,
                "anomaly_score_threshold": 0.7,
                "min_violation_duration_ms": 2000,
                "cooldown_sec": 300,
                "send_extra_snapshot_on_alarm": True,
            }
        """
        key = self._mk_key(uid, mid)
        now = time.time()

        with self._lock:
            old = self._by_key.get(key, {})

            rec: Dict[str, Any] = dict(old)  # 기존값 복사 후 필요한 필드만 덮어쓰기
            rec.setdefault("uid", uid)
            rec.setdefault("mid", mid)

            if light_schedule is not None:
                rec["light_schedule"] = light_schedule
            if snapshot_period_ms is not None:
                rec["snapshot_period_ms"] = snapshot_period_ms
            if status_period_ms is not None:
                rec["status_period_ms"] = status_period_ms
            if backup is not None:
                rec["backup"] = backup
            if ai_policy is not None:
                rec["ai_policy"] = ai_policy
            if extra:
                # extra 안의 키들은 그대로 병합
                for k, v in extra.items():
                    rec[k] = v

            rec["updated_at"] = now

            self._by_key[key] = rec
            self._atomic_save()
            return rec

    def get(self, uid: Optional[str], mid: Optional[int]) -> Optional[Dict[str, Any]]:
        key = self._mk_key(uid, mid)
        with self._lock:
            rec = self._by_key.get(key)
            return dict(rec) if rec is not None else None

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._by_key)

    def remove(self, uid: Optional[str], mid: Optional[int]) -> bool:
        key = self._mk_key(uid, mid)
        with self._lock:
            existed = key in self._by_key
            if existed:
                self._by_key.pop(key, None)
                self._atomic_save()
            return existed
