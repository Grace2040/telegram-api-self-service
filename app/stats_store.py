"""只保存汇总计数的 SQLite 存储，不记录手机号、验证码或凭据。"""
from contextlib import closing
from pathlib import Path
import sqlite3


SEND_FAILURE_KINDS = ('flood', 'network', 'phone_invalid', 'code_invalid', 'blocked',
                      'telegram_error', 'request_rejected', 'request_invalid', 'unknown')
CREATE_FAILURE_KINDS = ('code_invalid', 'flood', 'network', 'phone_invalid', 'blocked',
                        'telegram_error', 'request_rejected', 'state_expired', 'request_invalid', 'unknown')


def empty_snapshot() -> dict:
    return {'send_code_ok': 0, 'send_code_fail': {}, 'create_ok': 0,
            'create_existed': 0, 'create_fail': {}}


class StatsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('不支持的统计数据库版本')
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if (version == 0 and tables) or (version == 1 and tables != {'counters'}):
                raise ValueError('统计数据库结构不完整或文件用途不符')
            conn.execute('''CREATE TABLE IF NOT EXISTS counters (
                bucket TEXT NOT NULL,
                kind TEXT NOT NULL,
                value INTEGER NOT NULL CHECK(typeof(value) = 'integer' AND value >= 0),
                PRIMARY KEY (bucket, kind)
            )''')
            conn.execute('PRAGMA user_version = 1')
        self.snapshot()  # 文件损坏或数据不合法时明确失败，不重建、不清零。

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=5)
        try:
            conn.execute('PRAGMA synchronous = FULL')
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    @staticmethod
    def _validate(snapshot: dict) -> None:
        if set(snapshot) != set(empty_snapshot()):
            raise ValueError('统计字段不完整')
        counts = []
        for bucket, value in snapshot.items():
            if bucket.endswith('_fail'):
                allowed = SEND_FAILURE_KINDS if bucket == 'send_code_fail' else CREATE_FAILURE_KINDS
                if not isinstance(value, dict) or not set(value) <= set(allowed):
                    raise ValueError('统计失败原因不合法')
                counts.extend(value.values())
            else:
                counts.append(value)
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError('统计次数必须为非负整数')
        if snapshot['create_existed'] > snapshot['create_ok']:
            raise ValueError('查询成功次数超过总成功次数')

    def snapshot(self) -> dict:
        data = empty_snapshot()
        with closing(self._connect()) as conn:
            rows = conn.execute('SELECT bucket, kind, value FROM counters').fetchall()
        for bucket, kind, value in rows:
            if bucket not in data:
                raise ValueError('统计数据库包含未知字段')
            if bucket.endswith('_fail'):
                data[bucket][kind] = value
            elif kind == 'ok':
                data[bucket] = value
            else:
                raise ValueError('统计数据库包含未知计数类型')
        self._validate(data)
        return data

    def record(self, stage: str, *, success: bool, kind: str = 'unknown', existed: bool = False) -> None:
        if stage not in ('send', 'create'):
            raise ValueError('统计事件类型不合法')
        allowed = SEND_FAILURE_KINDS if stage == 'send' else CREATE_FAILURE_KINDS
        if not success and kind not in allowed:
            raise ValueError('统计事件类型不合法')
        if stage == 'send':
            events = [('send_code_ok', 'ok')] if success else [('send_code_fail', kind)]
        elif success:
            events = [('create_ok', 'ok')] + ([('create_existed', 'ok')] if existed else [])
        else:
            events = [('create_fail', kind)]
        # 查询成功的总数与子项在同一事务中提交，避免中途退出造成口径分叉。
        with closing(self._connect()) as conn, conn:
            conn.executemany('''INSERT INTO counters(bucket, kind, value) VALUES (?, ?, 1)
                ON CONFLICT(bucket, kind) DO UPDATE SET value = value + 1''', events)

    def import_snapshot(self, data: dict) -> None:
        """运维迁移入口：仅向空数据库一次性导入已有汇总，不覆盖已累计数据。"""
        self._validate(data)
        rows = []
        for bucket, value in data.items():
            if isinstance(value, dict):
                rows.extend((bucket, kind, count) for kind, count in value.items())
            else:
                rows.append((bucket, 'ok', value))
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT COUNT(*) FROM counters').fetchone()[0]:
                raise ValueError('统计数据库已有记录，拒绝重复导入')
            conn.executemany('INSERT INTO counters(bucket, kind, value) VALUES (?, ?, ?)', rows)
