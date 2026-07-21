# node_store.py (binary pickle 버전)
from __future__ import annotations
import os, time, tempfile, threading
from typing import Any, Dict, Optional, Sequence
import pickle


class NodeStore:
    """
    - 파일 1개에 최대 20개 노드의 '마지막 데이터'만 저장
    - 같은 uid/mid로 들어오면 덮어쓰기(최신 ts 유지)
    - 20개 초과 시 LRU(가장 오래 안 갱신된 항목) 제거
    - 바이너리(pickle) 파일 원자적 저장(임시파일 → rename)
    - 스레드 안전 (Lock)
    """

    def __init__(self, path: str = "nodes_store.bin", cap: int = 20):
        self.path = path
        self.cap = cap
        self._lock = threading.Lock()
        self._by_key: Dict[str, Dict[str, Any]] = {}
        self._load()
        if not os.path.exists(self.path):
            self._atomic_save()
    # -------------------------------------------------------------
    # 내부 유틸
    # -------------------------------------------------------------

    def all_nodes(self):
        """
        현재 저장된 모든 노드 상태를 리스트로 반환.
        각 원소는 upsert 때 넣었던 dict (uid, mid, mac, voltage, current, temperature, fft, ts 등).
        """
        with self._lock:
            # 얕은 복사 정도면 충분
            return list(self._by_key.values())
        
    def pending_nodes(self):
        with self._lock:
            return [rec for rec in self._by_key.values() if bool(rec.get("pending_send", 0))]

    def _mk_key(self, uid: Optional[str], mid: Optional[int]) -> str:
        """
        uid / mid 조합으로 내부 key 생성.
        둘 다 있으면 "uid:...|mid:..." 형태, 하나만 있으면 그 값만.
        """
        uid_part = f"uid:{uid}" if uid is not None else ""
        mid_part = f"mid:{mid}" if mid is not None else ""
        if uid_part and mid_part:
            return f"{uid_part}|{mid_part}"
        return uid_part or mid_part or "unknown"

    def _is_valid_uid(self, uid: Optional[str]) -> bool:
        """
        UID 문자열이 '정상적인 노드 UID'처럼 보이는지 간단히 검증.

        - None 이면 False
        - 길이: 24 또는 26 글자 (필요시 조정)
        - 전부 16진수 문자
        - 바이트에 CR/LF(0x0D, 0x0A)가 들어있으면 reject
        - 마지막 6바이트는 ASCII 숫자('0'~'9')라고 가정 (실제 UID 패턴 기준)
        """
        if uid is None:
            return False
        if not isinstance(uid, str):
            return False

        uid = uid.strip()
        if not uid:
            return False

        # 24자리 / 26자리 16진 UID만 허용 (필요하면 길이 조정 가능)
        if len(uid) not in (24, 26):
            return False

        try:
            b = bytes.fromhex(uid)
        except ValueError:
            # 16진수로 해석이 안 되면 잘못된 UID
            return False

        # CR(0x0D), LF(0x0A)가 섞여 있으면 명백히 깨진 값
        if any(ch in (0x0D, 0x0A) for ch in b):
            return False

        # 패턴 방어: 마지막 6바이트는 숫자 문자('0'~'9')라고 가정
        # 예: ".... 33 31 39 38 32 36" → "319826"
        tail = b[-6:]
        if not all(0x30 <= ch <= 0x39 for ch in tail):
            return False

        return True

    def _load(self) -> None:
        """바이너리(pickle) 파일에서 상태 로드."""
        if not os.path.exists(self.path):
            return

        try:
            with open(self.path, "rb") as f:
                obj = pickle.load(f)

            # {"version": 1, "saved_at": ..., "nodes": {...}} 형태 기대
            if isinstance(obj, dict) and "nodes" in obj:
                nodes = obj["nodes"]
                if isinstance(nodes, dict):
                    self._by_key = nodes
                else:
                    print("[NodeStore] invalid nodes type in file, ignoring")
            elif isinstance(obj, dict):
                # 구버전 포맷일 수도 있으니 그냥 dict 자체를 사용
                self._by_key = obj
            else:
                print("[NodeStore] unexpected file content, ignoring")
        except Exception as e:
            print(f"[NodeStore] load error: {e!r}")
            # 깨졌으면 그냥 빈 상태로 시작
            self._by_key = {}

    def _drop_other_records_with_same_mid(self, mid: Optional[int], keep_key: str) -> None:
        """
        새 uplink가 들어오면 같은 mid의 이전 레코드는 제거한다.
        같은 mid를 새 노드가 재사용할 때 최신 레코드가 우선되도록 한다.
        """
        if mid is None:
            return

        stale_keys = [
            rec_key
            for rec_key, rec in self._by_key.items()
            if rec_key != keep_key and rec.get("mid") == mid
        ]
        for rec_key in stale_keys:
            self._by_key.pop(rec_key, None)

    def _atomic_save(self) -> None:
        """현재 상태를 바이너리(pickle)로 원자적 저장."""
        tmp_dir = os.path.dirname(self.path) or "."
        os.makedirs(tmp_dir, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(prefix=".nodes_store_tmp", dir=tmp_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                obj = {
                    "version": 1,
                    "saved_at": time.time(),
                    "nodes": self._by_key,
                }
                pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, self.path)
        except Exception as e:
            print(f"[NodeStore] save error: {e!r}")
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    
    def flush_to_disk(self) -> None:
        with self._lock:
            self._atomic_save()

    # -------------------------------------------------------------
    # 퍼블릭 API
    # -------------------------------------------------------------
    def upsert(
            self,
            uid: Optional[str],
            mid: Optional[int],
            mac: Optional[str] = None,
            ultrasonic: Optional[float] = None,
            voltage: Optional[float] = None,
            current: Optional[float] = None,
            temperature: Optional[float] = None,
            light_on: Optional[int] = None,
            fft: Optional[Sequence[Sequence[float]]] = None,
            ts: Optional[float] = None,
            *,
            channel: Optional[int] = None,
            interval: Optional[int] = None,
            mid_assigned: Optional[bool] = None,
            pending_send: Optional[bool] = None,
            last_sent_ts: Optional[float] = None,
            last_snap_ts: Optional[float] = None,
            ai_valid: Optional[int] = None,
            ai_mse: Optional[float] = None,
            ai_pred: Optional[int] = None,
            last_good_temperature: Optional[float] = None,
            last_good_fft: Optional[Sequence[Sequence[float]]] = None,
            last_good_measurement_ts: Optional[float] = None,
            #clear_temperature: bool = False,
        ) -> Dict[str, Any]:

        if uid is not None and not self._is_valid_uid(uid):
            print(f"[NodeStore] ignore invalid uid: {uid!r}")
            # 저장은 하지 않고, 빈 dict 반환 (호출부에서 굳이 안 쓰면 됨)
            return {}
        
        key = self._mk_key(uid, mid)
        now = ts if ts is not None else time.time()

        with self._lock:
            old = self._by_key.get(key, {})
        
            rec: Dict[str, Any] = {
                "uid": uid if uid is not None else old.get("uid"),
                "mid": mid if mid is not None else old.get("mid"),
                "mac": mac if mac is not None else old.get("mac"),
                "ultrasonic": (
                    ultrasonic if ultrasonic is not None else old.get("ultrasonic")
                ),
                "voltage": voltage if voltage is not None else old.get("voltage"),
                "current": current if current is not None else old.get("current"),
                """ "temperature": (
                    None if clear_temperature
                    else temperature if temperature is not None
                    else old.get("temperature")
                ), """
                "temperature": (
                    temperature if temperature is not None else old.get("temperature")
                ),
                "light_on": (
                    light_on if light_on is not None else old.get("light_on")
                ),
                "fft": fft if fft is not None else old.get("fft"),
                "ts": now,
                "channel": (
                    channel if channel is not None else old.get("channel")
                ),
                "interval": (
                    int(interval) if interval is not None else old.get("interval")
                ),
                "mid_assigned": (
                    mid_assigned if mid_assigned is not None else old.get("mid_assigned")
                ),
                "pending_send": (
                    int(bool(pending_send)) if pending_send is not None else int(old.get("pending_send", 0))
                ),
                "last_sent_ts": (
                    float(last_sent_ts) if last_sent_ts is not None else old.get("last_sent_ts")
                ),
                "last_snap_ts": (
                    float(last_snap_ts) if last_snap_ts is not None else old.get("last_snap_ts")
                ),
                "ai_valid": (
                    int(ai_valid) if ai_valid is not None else old.get("ai_valid")
                ),
                "ai_mse": (
                    float(ai_mse) if ai_mse is not None else old.get("ai_mse")
                ),
                "ai_pred": (
                    int(ai_pred) if ai_pred is not None else old.get("ai_pred")
                ),
                "last_good_temperature": (
                    last_good_temperature
                    if last_good_temperature is not None
                    else old.get("last_good_temperature")
                ),
                "last_good_fft": (
                    last_good_fft
                    if last_good_fft is not None
                    else old.get("last_good_fft")
                ),
                "last_good_measurement_ts": (
                    float(last_good_measurement_ts)
                    if last_good_measurement_ts is not None
                    else old.get("last_good_measurement_ts")
                ),
            }

            self._by_key[key] = rec
            self._drop_other_records_with_same_mid(mid, keep_key=key)

            # 용량 초과 시 LRU 제거
            if len(self._by_key) > self.cap:
                victim_key = min(
                    self._by_key.items(), key=lambda kv: kv[1].get("ts", 0)
                )[0]
                if victim_key != key:
                    self._by_key.pop(victim_key, None)

            self._atomic_save()
            return rec

    def mark_sent_for_mids(self, mids: Sequence[int], sent_ts: Optional[float] = None) -> int:
        when = sent_ts if sent_ts is not None else time.time()
        targets = {int(mid) for mid in mids if mid is not None}
        if not targets:
            return 0

        updated = 0
        with self._lock:
            for rec in self._by_key.values():
                try:
                    mid = int(rec.get("mid") or 0)
                except (TypeError, ValueError):
                    continue
                if mid in targets:
                    rec["pending_send"] = 0
                    rec["last_sent_ts"] = when
                    updated += 1

            if updated:
                self._atomic_save()
        return updated

    def mark_pending_by_mid(self, mid: int, pending: bool = True) -> int:
        try:
            target_mid = int(mid or 0)
        except (TypeError, ValueError):
            return 0
        if target_mid <= 0:
            return 0

        updated = 0
        with self._lock:
            for rec in self._by_key.values():
                try:
                    rec_mid = int(rec.get("mid") or 0)
                except (TypeError, ValueError):
                    continue
                if rec_mid != target_mid:
                    continue
                new_pending = 1 if pending else 0
                if int(rec.get("pending_send", 0) or 0) != new_pending:
                    rec["pending_send"] = new_pending
                    updated += 1

            if updated:
                self._atomic_save()
        return updated

    def get_by_uid_mid(self, uid: Optional[str], mid: Optional[int]) -> Optional[Dict[str, Any]]:
        key = self._mk_key(uid, mid)
        with self._lock:
            rec = self._by_key.get(key)
            return dict(rec) if rec is not None else None

    def get_by_uid(self, uid: str) -> Optional[Dict[str, Any]]:
        """
        같은 uid라도 mid가 여러 개 있을 수 있으므로,
        uid가 같은 것들 중 ts가 가장 최근인 레코드를 반환.
        """
        with self._lock:
            candidates = [
                rec for rec in self._by_key.values()
                if rec.get("uid") == uid
            ]
            if not candidates:
                return None
            best = max(candidates, key=lambda r: r.get("ts", 0))
            return dict(best)
        
    def update_mid_chan(
            self,
            uid: str,
            new_mid: int,
            new_channel: Optional[int] = None,
        ) -> Optional[Dict[str, Any]]:
        """
        설치 모드에서 mid=0 이던 노드가 mid가 할당되었을 때
        NodeStore 엔트리를 옮기고(channel, mid_assigned도 갱신) 저장.
        """
        if not self._is_valid_uid(uid):
            print(f"[NodeStore] update_mid_chan ignore invalid uid: {uid!r}")
            return None
        
        with self._lock:
            # 1) uid가 같은 후보들을 모두 찾음
            candidates = [
                (key, rec)
                for key, rec in self._by_key.items()
                if rec.get("uid") == uid
            ]
            if not candidates:
                return None

            # ts 최신 것 하나 선택
            old_key, old_rec = max(
                candidates,
                key=lambda kv: kv[1].get("ts", 0)
            )

            # 2) 새 key 생성 (uid + new_mid)
            new_key = self._mk_key(uid, new_mid)

            # 3) 기존 기록을 복사해서 업데이트
            rec = dict(old_rec)
            rec["mid"] = new_mid
            rec["channel"] = new_channel if new_channel is not None else rec.get("channel")
            rec["mid_assigned"] = True
            rec["ts"] = time.time()

            # 4) 옮기기 (old_key 삭제 후 new_key에 넣기)
            self._by_key.pop(old_key, None)
            self._by_key[new_key] = rec

            # cap 체크는 없어도 거의 문제 없지만, 혹시 기존 로직 맞추고 싶으면 넣어도 됨
            if len(self._by_key) > self.cap:
                victim_key = min(
                    self._by_key.items(),
                    key=lambda kv: kv[1].get("ts", 0)
                )[0]
                if victim_key != new_key:
                    self._by_key.pop(victim_key, None)

            self._atomic_save()
            return dict(rec)

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            # 복사본 반환(외부에서 변조 방지)
            return dict(self._by_key)

    def remove(self, uid: Optional[str], mid: Optional[int]) -> bool:
        key = self._mk_key(uid, mid)
        with self._lock:
            existed = key in self._by_key
            if existed:
                self._by_key.pop(key, None)
                self._atomic_save()
            return existed
