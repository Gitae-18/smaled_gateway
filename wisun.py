# wisun.py (revised for CBOR TX, JSON/CBOR RX)

import serial, struct, cbor2, threading, queue, time, datetime, io, json
from typing import Optional, Tuple, Union, Iterable
from serial.serialutil import SerialException
from store_config import NodeConfigStore
import logging
try:
    from get_rtc import read_rtc_via_ioctl
except Exception:
    read_rtc_via_ioctl = None

try:
    from store import NodeStore
except Exception:
    NodeStore = None  # 선택사항

STX  = 0x02
SIG1 = 0xAA
SIG2 = 0xAB  # TX 채널에서 사용
ETX  = 0x03
_HEADER = bytes([STX, SIG1, SIG2])
ACK_T = 0x10
ACK_BIN_SIZE = 1 + 12 + 4 + 1 + 1 


def _calc_ck(sig1: int, sig2: int, length: int, data_field: bytes) -> int:
    """
    모듈 CK 규칙:
    CK = XOR(sig1, sig2, length, data_field 전체)
    data_field 는 [MID_L, MID_H, RXP, RESV, MAC(8), DATA...] 전부
    """
    cs = 0
    cs ^= sig1
    cs ^= sig2
    cs ^= length
    for b in data_field:
        cs ^= b
    return cs


def print_received_packet(prefix: str, data: Iterable[int]) -> None:
    try:
        ts = read_rtc_via_ioctl()
        if isinstance(ts, datetime.datetime):
            ts = ts.timestamp()
    except Exception:
        ts = time.time()

    lt = time.localtime(ts)
    timestamp = f"{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
    hex_part = " ".join(f"{b:02X}" for b in data)
    print(f"[{timestamp}] {prefix}{hex_part}")


class WiSunLink:
    """
    Gateway <-> Wi-SUN 모듈 직렬 링크.

    TX (Gateway → Node):
      Gateway가 직접 만드는 프레임:
        [STX][SIG1=0xAA][SIG2=0xAB][LEN][TMID_L][TMID_H][DATA...][CK][ETX]
      - LEN = DATA 길이 (CBOR payload 길이)
      - CK  = XOR(SIG1, SIG2, LEN, DATA bytes)

    RX (Node → Gateway, 모듈이 가공해서 전달):
      [STX][SIG1=0xAA][SIG2=0xAA][LEN]
      [MID_L][MID_H][RXP][RESV][MAC(8)][DATA(LEN bytes)][CK][ETX]

      전체 길이 = 18 + LEN
    """

    def __init__(self, port: str, baudrate: int = 9600, store: Optional["NodeStore"] = None):
        self.uart = serial.Serial(port, baudrate=baudrate, timeout=0)
        self._rx_buf = bytearray()
        # mid, parsed(dict or bytes), ts, mac
        self._q: "queue.Queue[Tuple[int, Union[dict, bytes], int, Optional[str]]]" = queue.Queue(maxsize=200)
        self._store = store
        self._alive = True
        self.timeout = 0.1 
        self.port = port
        self.baudrate = baudrate
        self._rx_thr = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thr.start()
        self._at_q = queue.Queue(maxsize=200)
        self._at_lock = threading.Lock()
        self._at_text_buf = ""
        self._frame_error_handlers = []
        self.log = logging.getLogger("gw")
        self.log.info("WiSunLink init port=%s baud=%d", self.port, self.baudrate)
    # ───────────────── TX ─────────────────
    def _reopen_uart(self):
        self.uart = serial.Serial(port=self.port, baudrate=self.baudrate, timeout=self.timeout)

    def send_at_command(self, cmd: str) -> None:
        """Wi-SUN 모듈 AT 명령용 (디버그 등)"""
        self.uart.write((cmd + "\r\n").encode("ascii", errors="ignore"))

    """ def send_wisun_packet(self, mid: int, payload: bytes) -> None:
        # TMID
        tmid_l = mid & 0xFF
        tmid_h = (mid >> 8) & 0xFF

        data_field = payload
        length = len(data_field)

        ck = _calc_ck(SIG1, SIG2, length, data_field)

        frame = bytes([
            STX,
            SIG1,
            SIG2,
            length,
            tmid_l,
            tmid_h,
        ]) + data_field + bytes([ck, ETX])

        # print("[UART TX FRAME]", frame.hex(" "))
        self.uart.write(frame)

    def send_cmd_bytes(self, mid: int, cmd: int,
                       msg_id: int = 0,
                       flags: int = 0,
                       extra: bytes = b"") -> None:

        payload = struct.pack(">BBH", cmd, flags, msg_id & 0xFFFF) + extra
        self.send_wisun_packet(mid, payload) """
    def send_wisun_packet(self, tmid_header: int, data_field: bytes) -> None:
        """
        tmid_header:
            Wi-SUN 헤더 MID. RX에서는 Source MID가 됨.
            노드에서는 payload MID만 쓰므로 사실상 의미 없음 → 0 권장
        """

        tmid_l = tmid_header & 0xFF
        tmid_h = (tmid_header >> 8) & 0xFF

        length = len(data_field)
        ck = _calc_ck(SIG1, SIG2, length, data_field)

        frame = bytes([
            STX,
            SIG1,
            SIG2,
            length,
            tmid_l,
            tmid_h,
        ]) + data_field + bytes([ck, ETX])
        print("[UART TX FRAME]", frame.hex(" "))
        self.uart.write(frame)


    def send_cmd_bytes(self,
                   target_mid: int,
                   cmd: int,
                   msg_id: int = 0,
                   flags: int = 0,
                   extra: bytes = b"",
                   ttl: int = 10) -> None:

        payload = bytearray()

        # [0..1] target_mid
        payload.append(target_mid & 0xFF)
        payload.append((target_mid >> 8) & 0xFF)

        # [2] TTL
        payload.append(ttl & 0xFF)

        # [3] CMD
        payload.append(cmd & 0xFF)

        # [4] FLAGS
        payload.append(flags & 0xFF)

        # [5..6] msg_id (Hi, Lo)
        payload.append((msg_id >> 8) & 0xFF)
        payload.append(msg_id & 0xFF)

        if extra:
            payload.extend(extra)

        # 헤더 MID는 "보내는 쪽 MID" (게이트웨이 모듈 MID)
        # 지금처럼 0x0000 혹은 gw_mid 쓰면 됨
        self.send_wisun_packet(0x0000, bytes(payload))


    def send(self, mid: int, payload: bytes):
        """기존 cmd_router의 self.wisun.send(mid, payload) 호환용."""
        return self.send_wisun_packet(mid, payload)

    # ───────────────── RX pull API ─────────────────

    def add_frame_error_handler(self, fn):
        if callable(fn):
            self._frame_error_handlers.append(fn)

    def _emit_frame_error(self, kind: str, *, frame: bytes | None = None,
                          mid: int | None = None, detail: str | None = None):
        ts = self._get_timestamp()
        payload_hex = frame.hex(" ") if isinstance(frame, (bytes, bytearray)) else None
        event = {
            "kind": kind,
            "mid": mid,
            "ts": ts,
            "detail": detail,
            "payload_hex": payload_hex,
        }
        for fn in list(self._frame_error_handlers):
            try:
                fn(event)
            except Exception as e:
                self.log.warning("wisun frame error handler failed err=%r event=%r", e, event)

    def get_packet_nowait(self) -> Optional[Tuple[int, Union[dict, bytes], int, Optional[str]]]:
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    # ───────────────── 내부 RX 루프 ─────────────────

    def _feed_text_bytes(self, b: bytes):
        # printable ASCII + CR/LF/TAB만 살림
        s = bytes(x for x in b if (32 <= x < 127) or x in (9, 10, 13)).decode("ascii", errors="ignore")
        if not s:
            return

        self._at_text_buf += s
        while "\n" in self._at_text_buf:
            line, self._at_text_buf = self._at_text_buf.split("\n", 1)
            line = line.strip("\r").strip()
            if not line:
                continue
            print("[UART RX TEXT]", line)
            try:
                self._at_q.put_nowait(line)
            except queue.Full:
                pass

    def _ensure_uart_open(self):
        # 이미 열려있으면 OK
        if getattr(self, "uart", None) is not None and self.uart.is_open:
            return True

        # 필요 정보: 포트/baud/timeout (너 코드에 있는 값으로)
        try:
            self.uart = serial.Serial(
                port=self.port,          
                baudrate=self.baudrate,  
                timeout=self.timeout
            )
            return True
        except Exception as e:
            print(f"[UART] reopen failed: {e}")
            return False
    
    def _rx_loop(self):
        idle_sleep = 0.01          # 빈 read일 때 CPU 과점유 방지
        reopen_backoff = 1.0       # 재연결 실패 시 대기
        max_rx_buf = 4096          # 바이너리 버퍼 보호(필요시 조절)

        while self._alive:            
            try:
                if getattr(self, "uart", None) is None or not self.uart.is_open:
                    try:
                        self._reopen_uart()
                        try:
                            self.uart.reset_input_buffer()
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"[UART] reopen failed: {e}")
                        self.log.warning("wisun uart reopen failed port=%s err=%r", self.port, e)
                        time.sleep(reopen_backoff)
                        continue
            except Exception:
                # uart 객체 자체가 이상한 경우도 방어
                time.sleep(reopen_backoff)
                continue

            # 1) read()는 예외만 disconnect로 취급
            try:
                chunk = self.uart.read(256)
                if not chunk:
                    time.sleep(idle_sleep)
                    continue
                
            except SerialException as e:
                print(f"[UART] SerialException in read(): {e}")
                self.log.warning("UART SerialException in read(): %r (port=%s)", e, self.port)
                try:
                    self.uart.close()
                except Exception:
                    pass
                time.sleep(reopen_backoff)
                continue

            # 2) 빈 read는 '데이터 없음'일 뿐(정상) → 짧게 쉬고 다음 루프
            if not chunk:
                time.sleep(idle_sleep)
                continue 

            # 1) chunk 내부에서 STX를 기준으로 텍스트/바이너리를 분리
            i = 0
            while True:
                j = chunk.find(bytes([STX]), i)
                if j == -1:
                    # STX가 더 이상 없으면 남은 건 텍스트 후보
                    tail = chunk[i:]
                    if tail and (STX not in self._rx_buf):
                        self._feed_text_bytes(tail)
                    else:
                        # 바이너리 버퍼가 이미 진행 중이면 tail도 바이너리로 붙임
                        self._rx_buf.extend(tail)
                        self._drain_frames()
                    break

                # STX 이전 바이트는 텍스트 후보
                head = chunk[i:j]
                if head and (STX not in self._rx_buf):
                    self._feed_text_bytes(head)
                else:
                    self._rx_buf.extend(head)
                    self._drain_frames()

                # STX부터 끝까지는 일단 바이너리 버퍼로
                self._rx_buf.extend(chunk[j:])
                self._drain_frames()
                break  # chunk[j:]를 다 넘겼으니 종료

            # 너무 오래 STX 없이 바이너리가 쌓이면 버퍼 보호
            if len(self._rx_buf) > max_rx_buf:            
                if STX not in self._rx_buf:
                    self._rx_buf.clear()
                else:
                    self._rx_buf = self._rx_buf[-2048:]

    def at_request(self, cmd: str, timeout: float = 2.0):
        """
        AT 커맨드 보내고, OK/ERROR 나올 때까지 라인 수집.
        return: (ok: bool, lines: list[str])
        """
        with self._at_lock:
            # 큐 비우기
            while True:
                try:
                    self._at_q.get_nowait()
                except queue.Empty:
                    break

            # 전송
            self.send_at_command(cmd)

            lines = []
            t_end = time.time() + timeout
            ok = False

            while time.time() < t_end:
                remain = t_end - time.time()
                try:
                    line = self._at_q.get(timeout=max(0.05, remain))
                except queue.Empty:
                    continue

                lines.append(line)

                u = line.upper()
                if u == "OK":
                    ok = True
                    break
                if u.startswith("ERROR"):
                    ok = False
                    break

            return ok, lines

    def at_get_cfg(self, timeout: float = 2.0) -> dict:
        ok, lines = self.at_request("AT+CFG?", timeout=timeout)

        # 마지막 OK/ERROR는 보기 싫으면 제거(선택)
        clean = [ln for ln in lines if ln.upper() not in ("OK",) and not ln.upper().startswith("ERROR")]

        return {
            "cmd": "AT+CFG?",
            "ok": ok,
            "lines": clean,               # 라인 배열
            "raw": "\n".join(clean),      # 통짜 문자열도 같이
        }
    
    def _get_timestamp(self) -> int:
        try:
            rtc_val = read_rtc_via_ioctl()
            if isinstance(rtc_val, datetime.datetime):
                return int(rtc_val.timestamp())
            return int(rtc_val)
        except Exception:
            return int(time.time())
        
    def _try_decode_snap_bin(self, data_bytes: bytes) -> Optional[dict]:
        """
        노드에서 보내는 SnapBin_t 바이너리 포맷 시도 디코딩.

        C struct 기준:
        typedef struct __attribute__((packed)) {
            uint8_t  t;         // 0x01 = snap (예)
            uint8_t  uid[12];
            float    volt;
            float    curr;
            float    temp;
            uint8_t  fft_count;
            struct {
                float freq;
                float amp;
            } fft[SNAP_FFT_PAIRS];
        } SnapBin_t;
        """
        # 최소 헤더(1 + 12 + 4*3 + 1 = 26바이트) 없으면 스킵
        if len(data_bytes) < 26:
            return None

        t = data_bytes[0]

        # t 값으로 우리가 정의한 타입만 인정 (예: 0x01 또는 's')
        if t not in (0x01, ord("s"), ord("S")):
            return None

        try:
            uid = data_bytes[1:13]  # 12 bytes

            # little-endian float 3개
            volt, curr, temp = struct.unpack_from("<fff", data_bytes, 13)

            fft_count = data_bytes[25]
            pos = 26

            remain = len(data_bytes) - pos
            max_pairs = remain // 8  # each pair = 8 bytes (float2)

            n = min(fft_count, max_pairs)
            fft = []

            for _ in range(n):
                freq, amp = struct.unpack_from("<ff", data_bytes, pos)
                pos += 8
                fft.append([freq, amp])

            parsed = {
                "t": "snap",              # 논리 타입 이름
                "t_raw": t,               # 실제 바이너리 값
                "uid": uid.hex(),         # 12바이트 UID를 hex string으로
                "volt": volt,
                "curr": curr,
                "temp": temp,
                "fft_count": n,
                "fft": fft,
            }

            print(
                f"[GW] SnapBin decoded ok: len={len(data_bytes)} "
                f"uid={parsed['uid']} fft_count={n}"
            )
            return parsed

        except Exception as e:
            print(f"[GW] SnapBin decode error: {e!r} len={len(data_bytes)} raw= {data_bytes.hex(' ')}")
            return None

    def _decode_payload(self, data_bytes: bytes, mid: int, mac_str: str, rxp: int, ts: int) -> Union[dict, bytes]:
        print(f"[WISUN DECODE IN] len={len(data_bytes)} t=0x{data_bytes[0]:02X} head={data_bytes[:16].hex(' ')}")
        if data_bytes and data_bytes[0] in (0x10, 0x01, 0x02, 0x15, 0x24, 0x40):
            return data_bytes
    
        if data_bytes and len(data_bytes) >= ACK_BIN_SIZE and data_bytes[0] == ACK_T:
        # 여기서 굳이 unpack 하지 말고, raw bytes를 그대로 넘겨야
        # on_wisun_uplink()의 ACK 분기가 동작함
            return data_bytes
    

        # 0) SnapBin_t 바이너리 시도
        snap_parsed = self._try_decode_snap_bin(data_bytes)
        if snap_parsed is not None:
            return snap_parsed

        text = None
        if data_bytes:
            try:
                text = data_bytes.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                text = data_bytes.decode("utf-8", errors="ignore")

        # 1) JSON 파싱(그대로)
        if text and text.strip().startswith(("{", "[")):
            try:
                parsed = json.loads(text)
                print(f"[GW] JSON decoded ok: ts={ts} len={len(data_bytes)} type={type(parsed)}")
                return parsed
            except json.JSONDecodeError as e:
                print(f"[GW] JSON decode error: {e!r}")
                # 1-1) { ... } 슬랩만 다시 시도
                try:
                    start = text.find("{")
                    end   = text.rfind("}")
                    if start != -1 and end != -1 and end > start:
                        slab = text[start:end+1]
                        parsed2 = json.loads(slab)
                        print(f"[GW] JSON decoded from slab ok: ts={ts} len={len(slab)}")
                        return parsed2
                except Exception as e2:
                    print(f"[GW] JSON slab decode error: {e2!r}")

        # 2) CBOR 시도 (예전 포맷 호환용, 필요 없으면 이 블록 통째로 삭제해도 됨)
        """ try:
            dec = cbor2.CBORDecoder(io.BytesIO(data_bytes))
            parsed = dec.decode()
            if isinstance(parsed, list):
                print(f"[GW] CBOR decoded ok: ts={ts} len={len(data_bytes)} items={len(parsed)}")
            else:
                print(f"[GW] CBOR decoded ok: ts={ts} len={len(data_bytes)} type={type(parsed)}")
            return parsed
        except Exception as e:
            print(f"[GW] CBOR decode error: {e} len={len(data_bytes)} raw= {data_bytes.hex(' ')}") """

        # 3) 최후 수단: 깨진 문자열에서 필요한 값만 regex로 추출
        result = {
            "raw_text": text if text is not None else "",
            "raw_hex": data_bytes.hex(" "),
            "mid": mid,
            "mac": mac_str,
            "rxp": rxp,
            "partial": True,   # 부분 파싱임을 표시
        }

        if text:
            import re

            # "t":"sn"
            m_t = re.search(r'"t"\s*:\s*"([^"]*)"', text)
            if m_t:
                result["t"] = m_t.group(1)

            # "u":"4200..."
            m_u = re.search(r'"u"\s*:\s*"([^"]*)"', text)
            if m_u:
                result["u"] = m_u.group(1)

            # FFT 형태: [231.9,0.00685] 같은 쌍들
            pairs = re.findall(r'\[([0-9.]+)\s*,\s*([0-9.]+)\]', text)
            if pairs:
                fft = []
                for a, b in pairs:
                    try:
                        fft.append([float(a), float(b)])
                    except ValueError:
                        continue
                if fft:
                    result["fft"] = fft
        print(f"[WISUN DECODE OUT] type={type(result)}")
        return result



    def _drain_frames(self):
        buf = self._rx_buf        

        while True:
            # 1) STX 찾기
            try:
                i = buf.index(STX)  # 0x02
            except ValueError:
                buf.clear()
                return

            if i > 0:
                del buf[:i]

            # 최소 공통 헤더: STX SIG1 SIG2 LEN MID(2) => 6바이트
            if len(buf) < 6:
                return

            sig1 = buf[1]
            sig2 = buf[2]

            # 2) SIG 검증
            if sig1 != 0xAA or sig2 not in (0xAA, 0xAB):
                del buf[0]  # 1바이트 밀고 재탐색
                continue

            declared_dl = int(buf[3])  # LEN

            # 3) SIG2별 최소 길이/기대 길이 계산
            """ if sig2 == 0xAA:
                # RX는 MAC까지 최소 16바이트 필요
                if len(buf) < 16:
                    return
                expected_len = declared_dl + 18
            else:
                expected_len = declared_dl + 8 """
            if sig2 == 0xAA:
                data_offset = 16
            else:
                data_offset = 6

            expected_len = data_offset + declared_dl + 2 
            
            if expected_len <= 0 or expected_len > 4096:
                # 말도 안 되는 LEN이면 STX 한 바이트 밀고 재동기화
                del buf[0]
                continue

            if len(buf) < expected_len:
                return

            frame = bytes(buf[:expected_len])
            del buf[:expected_len]
            frame_mid = (frame[4] | (frame[5] << 8)) if len(frame) >= 6 else None

            # 4) ETX 검사 실패 시 재동기화 (핵심)
            if frame[-1] != ETX:
                print(f"[GW] invalid ETX: {frame[-1]:02X}, drop frame")
                self.log.warning("wisun drop invalid etx got=%02X expected=%02X frame_len=%d",
                     frame[-1], ETX, len(frame))
                self._emit_frame_error(
                    "invalid_etx",
                    frame=frame,
                    mid=frame_mid,
                    detail=f"got={frame[-1]:02X} expected={ETX:02X}",
                )
                j = frame[1:].find(bytes([STX]))
                if j >= 0:
                    buf[:0] = frame[1 + j:]
                continue
             
            ck_got = frame[-2]

            if sig2 == 0xAA:
                # CK 계산에 들어가는 data_field_full = [MID_L..DATA...] (MID부터 DATA 끝까지)
                data_field_full = frame[4:-2]   # MID_L ... DATA ... (CK/ETX 제외)
            else:
                # TX도 동일 규칙이면 MID부터 DATA 끝까지
                data_field_full = frame[4:-2]

            ck_exp = _calc_ck(sig1, sig2, declared_dl, data_field_full)

            if ck_got != ck_exp:
                print(f"[GW] CK mismatch drop: got={ck_got:02X} exp={ck_exp:02X} len={len(frame)}")
                self.log.warning("wisun drop ck mismatch got=%02X exp=%02X frame_len=%d",
                                 ck_got, ck_exp, len(frame))
                self._emit_frame_error(
                    "ck_mismatch",
                    frame=frame,
                    mid=frame_mid,
                    detail=f"got={ck_got:02X} expected={ck_exp:02X}",
                )

                # 재동기화: frame 내부 다음 STX 찾아서 되돌리기
                j = frame[1:].find(bytes([STX]))
                if j >= 0:
                    buf[:0] = frame[1 + j:]
                continue
            # 5) 필드 추출
            if sig2 == 0xAA:
                mid = frame[4] | (frame[5] << 8)
                # rxp = frame[6]
                mac_str = frame[8:16].hex()
                data_field = frame[16:-2]  # DATA(LEN)
            else:
                mid = frame[4] | (frame[5] << 8)
                mac_str = None
                data_field = frame[6:-2]
            print("[UART RX FRAME]", frame.hex(" "))
            print("[UART RX PAYLOAD]", data_field.hex(" "))
            frame_data_hex = frame[16:-2].hex(" ") if sig2 == 0xAA else frame[6:-2].hex(" ")
            data_head_hex = data_field[:16].hex(" ")
            data_tail_hex = data_field[-22:].hex(" ") if len(data_field) >= 22 else data_field.hex(" ")
            print(
                f"[UART RX DATA CHECK] mid={mid} declared_dl={declared_dl} actual_dl={len(data_field)} "
                f"frame_data_hex={frame_data_hex} data_head_hex={data_head_hex} data_tail_hex={data_tail_hex}",
                flush=True,
            )
            if len(data_field) != declared_dl:
                print(f"[GW] LEN mismatch: LEN={declared_dl} data={len(data_field)} drop")
                self.log.warning("wisun drop len mismatch declared=%d actual=%d",
                     declared_dl, len(data_field))
                self._emit_frame_error(
                    "len_mismatch",
                    frame=frame,
                    mid=mid,
                    detail=f"declared={declared_dl} actual={len(data_field)}",
                )
                #buf[:0] = frame[1:]
                nxt = frame[1:].find(bytes([STX]))
                if nxt >= 0:
                    buf[:0] = frame[1+nxt:]
                continue

            ts = self._get_timestamp()
            try:
                self._q.put_nowait((mid, data_field, ts, mac_str))
            except queue.Full:                
                self.log.warning("wisun drop queue full qsize=%d", self._q.qsize())            
                continue





    def close(self):
        self._alive = False
        try:
            self._rx_thr.join(timeout=0.5)
        except Exception:
            pass
        try:
            self.uart.close()
        except Exception:
            pass
