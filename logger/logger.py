# logger/logger.py
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Optional


class OnlyLevelOrAboveFilter(logging.Filter):
    """level 이상만 통과"""
    def __init__(self, min_level: int):
        super().__init__()
        self.min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.min_level


class DropNoisyFilter(logging.Filter):
    """
    너무 자주 찍히는 반복 로그를 파일에서만 걸러내고 싶을 때 사용.
    (필요 없으면 제거해도 됨)
    """
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()

        # 예시: 너무 자주 발생하는 heartbeat 같은 걸 파일에서는 제외
        # if "[HB]" in msg:
        #     return False

        return True


def _make_rotating_file_handler(
    filepath: Path,
    level: int,
    max_bytes: int,
    backup_count: int,
    fmt: logging.Formatter,
    extra_filter: Optional[logging.Filter] = None,
) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        filepath,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(fmt)
    if extra_filter:
        handler.addFilter(extra_filter)
    return handler


def setup_logger(
    name: str = "gw",
    log_dir: str | Path = "logs",
    console_level: int = logging.INFO,
    file_level: int = logging.INFO,
    error_file_level: int = logging.WARNING,
    max_bytes: int = 2 * 1024 * 1024,   # 2MB
    backup_count: int = 5,              # 5개 보관 (총 ~10MB 수준)
    enable_raw: bool = False,
) -> logging.Logger:
    """
    gateway 전체에서 공통으로 쓰는 로거 설정.
    - 콘솔: console_level 이상
    - 파일(gw.log): file_level 이상
    - 에러파일(gw.err.log): error_file_level 이상
    - RAW 파일(gw.raw.log): enable_raw=True 일 때만
    """

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # 핸들러에서 컷; 로거는 넉넉히
    logger.propagate = False

    # 중복 핸들러 방지 (재실행/재import 시)
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1) 콘솔 핸들러
    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 2) 일반 파일 핸들러
    fh = _make_rotating_file_handler(
        filepath=log_dir / "gw.log",
        level=file_level,
        max_bytes=max_bytes,
        backup_count=backup_count,
        fmt=fmt,
        extra_filter=DropNoisyFilter(),
    )
    logger.addHandler(fh)

    # 3) 에러 전용 파일 핸들러 (WARNING 이상만)
    eh = _make_rotating_file_handler(
        filepath=log_dir / "gw.err.log",
        level=logging.DEBUG,  # 필터로 컷
        max_bytes=max_bytes,
        backup_count=max(backup_count, 10),  # 에러는 좀 더 보관해도 됨
        fmt=fmt,
        extra_filter=OnlyLevelOrAboveFilter(error_file_level),
    )
    logger.addHandler(eh)

    # 4) RAW/HEX 전용 (옵션)
    if enable_raw:
        rawh = _make_rotating_file_handler(
            filepath=log_dir / "gw.raw.log",
            level=logging.DEBUG,
            max_bytes=max_bytes,
            backup_count=2,  # RAW는 보관 짧게
            fmt=fmt,
        )
        # RAW는 "raw" 로거로만 찍게 할 거라면 이렇게 이름 필터를 걸 수도 있음
        # rawh.addFilter(lambda r: r.name.endswith(".raw"))
        logger.addHandler(rawh)

    logger.info("logger initialized (log_dir=%s)", str(log_dir))
    return logger


def get_logger(name: str = "gw") -> logging.Logger:
    return logging.getLogger(name)
