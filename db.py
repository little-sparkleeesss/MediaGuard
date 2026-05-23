# db.py — SQLAlchemy checkpoint manager
#
# 使用独立的 writer 线程异步写入数据库，避免阻塞工作线程。
# 支持 SQLite（默认）和 PostgreSQL（通过 --db-url 传入连接字符串）。
# SQLite 自动开启 WAL + NORMAL synchronous 以提升并发写入性能。
# PostgreSQL 使用 ON CONFLICT 实现 upsert。

import sys
import json
import time
import queue
import threading

import sqlalchemy as sa

from config import BATCH_COMMIT_SIZE


class FlushRequest:
    """发送给 writer 线程的刷新请求，携带一个 Event 用于同步等待。"""
    def __init__(self):
        self.done = threading.Event()


class CheckpointManager:
    """断点续传管理器。
    
    对外接口：
        is_done(path)   -> 检查是否已处理
        get_done_paths() -> 批量获取已处理路径集合（性能优化）
        enqueue(path, status, issues, warnings) -> 入队待写入
        flush()          -> 阻塞等待队列清空
        get_all()        -> 获取全部记录
        clear()          -> 清空所有记录
        close()          -> 安全关闭
    """

    def __init__(self, db_url, retry_failures=False, engine_kwargs=None):
        self.db_url = db_url
        # retry_failures: True 时仅跳过 success+warning，失败/中断/未知都重新检查
        self.retry_failures = retry_failures
        # 有界队列（10000）：防止写入跟不上时无限吃内存
        self._queue = queue.Queue(maxsize=10000)
        # 读连接按线程隔离，避免跨线程共享 SQLite 连接
        self._read_local = threading.local()
        self._read_lock = threading.Lock()
        self._fatal_lock = threading.Lock()
        self._fatal_error = None

        ekw = dict(engine_kwargs) if engine_kwargs else {}
        # SQLite 特殊处理：允许多线程访问 + 不使用连接池（NullPool）
        if db_url.startswith('sqlite'):
            ekw.setdefault('connect_args', {})
            ekw['connect_args']['check_same_thread'] = False
            ekw.setdefault('poolclass', sa.pool.NullPool)

        self._engine = sa.create_engine(db_url, **ekw)
        self._is_sqlite = 'sqlite' in self._engine.dialect.name

        # 同步建表后再启动 writer 线程，避免 is_done 抢先查到空表
        with self._engine.begin() as conn:
            self._init_db(conn)

        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True
        )
        self._writer_thread.start()

    def _check_fatal(self):
        """所有公开方法入口检查是否有不可恢复的数据库错误。"""
        with self._fatal_lock:
            if self._fatal_error:
                raise RuntimeError(
                    f"[Checkpoint致命错误] {self._fatal_error}"
                )

    def _init_db(self, conn):
        """建表（幂等）。SQLite 额外开启 WAL + NORMAL。"""
        if self._is_sqlite:
            conn.execute(sa.text("PRAGMA journal_mode=WAL"))
            conn.execute(sa.text("PRAGMA synchronous=NORMAL"))
            conn.execute(sa.text("""
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    issues TEXT,
                    warnings TEXT,
                    updated_at INTEGER NOT NULL
                )
            """))
        else:
            conn.execute(sa.text("""
                CREATE TABLE IF NOT EXISTS files (
                    id BIGSERIAL PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    status VARCHAR(32) NOT NULL,
                    issues TEXT,
                    warnings TEXT,
                    updated_at BIGINT NOT NULL
                )
            """))
        conn.commit()

    def _upsert(self, conn, path, status, issues, warnings, ts):
        """插入或更新一条记录。SQLite/PG 使用各自的 upsert 语法。"""
        params = {
            'p': path, 's': status,
            'i': json.dumps(issues, ensure_ascii=False),
            'w': json.dumps(warnings, ensure_ascii=False),
            't': ts
        }
        if self._is_sqlite:
            conn.execute(sa.text("""
                INSERT INTO files
                (path, status, issues, warnings, updated_at)
                VALUES (:p, :s, :i, :w, :t)
                ON CONFLICT(path) DO UPDATE SET
                    status = EXCLUDED.status,
                    issues = EXCLUDED.issues,
                    warnings = EXCLUDED.warnings,
                    updated_at = EXCLUDED.updated_at
            """), params)
        else:
            conn.execute(sa.text("""
                INSERT INTO files
                (path, status, issues, warnings, updated_at)
                VALUES (:p, :s, :i, :w, :t)
                ON CONFLICT (path) DO UPDATE SET
                    status = EXCLUDED.status,
                    issues = EXCLUDED.issues,
                    warnings = EXCLUDED.warnings,
                    updated_at = EXCLUDED.updated_at
            """), params)

    def _writer_loop(self):
        """后台线程：不断从队列取出记录并批量提交。"""
        conn = self._engine.connect()

        pending = 0
        while True:
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            # None 信号：安全退出
            if item is None:
                break

            # FlushRequest：先提交已有数据，再唤醒等待者
            if isinstance(item, FlushRequest):
                if pending > 0:
                    try:
                        conn.commit()
                    except Exception as e:
                        with self._fatal_lock:
                            self._fatal_error = e
                        item.done.set()
                        break
                    pending = 0
                item.done.set()
                continue

            path, status, issues, warnings = item
            ts = int(time.time())
            try:
                self._upsert(conn, path, status, issues, warnings, ts)
                pending += 1
                if pending >= BATCH_COMMIT_SIZE:
                    try:
                        conn.commit()
                    except Exception as e:
                        print(f"[Checkpoint写入失败] {e}", file=sys.stderr)
                        with self._fatal_lock:
                            self._fatal_error = e
                        pending = 0
                        break
                    pending = 0
            except Exception as e:
                print(f"[Checkpoint写入失败] {e}", file=sys.stderr)
                with self._fatal_lock:
                    self._fatal_error = e
                # 致命错误后清空队列中所有 FlushRequest，避免永久阻塞
                while True:
                    try:
                        pending_item = self._queue.get_nowait()
                        if isinstance(pending_item, FlushRequest):
                            pending_item.done.set()
                    except queue.Empty:
                        break
                break

        # 收尾：提交残留、回滚失败事务、关闭连接
        with self._fatal_lock:
            had_fatal = self._fatal_error is not None

        if pending > 0 and not had_fatal:
            try:
                conn.commit()
            except Exception as e:
                with self._fatal_lock:
                    self._fatal_error = e

        if had_fatal:
            try:
                conn.rollback()
            except Exception:
                pass

        conn.close()

    def _get_read_conn(self):
        """获取当前线程的读连接（线程本地，自动创建）。"""
        if not hasattr(self._read_local, 'conn'):
            self._read_local.conn = self._engine.connect()
        return self._read_local.conn

    def is_done(self, path):
        """检查单个路径是否已处理过。大量文件时应使用 get_done_paths()。"""
        self._check_fatal()
        with self._read_lock:
            conn = self._get_read_conn()
            cur = conn.execute(
                sa.text("SELECT status FROM files WHERE path = :p"),
                {'p': path}
            )
            row = cur.fetchone()
            if not row:
                return False
            if self.retry_failures:
                return row[0] in ('success', 'warning')
            return True

    def get_done_paths(self):
        """一次性拉取所有已处理路径（替代 N 次 is_done 调用，避免 N+1 查询）。"""
        with self._read_lock:
            conn = self._get_read_conn()
            if self.retry_failures:
                cur = conn.execute(
                    sa.text("SELECT path FROM files WHERE status IN ('success', 'warning')")
                )
            else:
                cur = conn.execute(sa.text("SELECT path FROM files"))
            return {row[0] for row in cur}

    def enqueue(self, path, status, issues, warnings):
        """队列满时降级为同步直写（带背压，不丢数据也不阻塞线程池）。"""
        self._check_fatal()
        try:
            self._queue.put(
                (path, status, issues, warnings), timeout=2
            )
        except queue.Full:
            print(f"[警告] 检查点队列满，同步写入 {path}", file=sys.stderr)
            try:
                with self._engine.begin() as conn:
                    self._upsert(
                        conn, path, status, issues, warnings, int(time.time())
                    )
            except Exception as e:
                print(f"[同步写入失败] {e}", file=sys.stderr)

    def flush(self):
        """向 writer 发送刷新请求并阻塞等待完成。30 秒超时抛异常。"""
        self._check_fatal()
        req = FlushRequest()
        try:
            self._queue.put(req, timeout=5)
        except queue.Full:
            self._check_fatal()
            raise RuntimeError("checkpoint queue 已满，writer thread 无响应")
        if not req.done.wait(timeout=30):
            self._check_fatal()
            raise RuntimeError("checkpoint writer thread 无响应")

    def get_all(self):
        """先 flush 确保数据落盘，再返回全部记录。"""
        self.flush()

        with self._read_lock:
            conn = self._get_read_conn()
            cur = conn.execute(
                sa.text("SELECT path, status, issues, warnings FROM files")
            )
            data = {}
            for row in cur.fetchall():
                try:
                    issues = json.loads(row[2]) if row[2] else []
                except Exception:
                    issues = []
                try:
                    warnings = json.loads(row[3]) if row[3] else []
                except Exception:
                    warnings = []
                data[row[0]] = {
                    "status": row[1],
                    "issues": issues,
                    "warnings": warnings
                }
            return data

    def clear(self):
        """删除全部记录（--recheck 时调用）。"""
        self._check_fatal()
        with self._engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM files"))

    def close(self):
        """安全关闭：flush → 发送退出信号 → join writer thread → dispose engine。"""
        try:
            self.flush()
        except Exception:
            pass

        try:
            self._queue.put(None, timeout=5)
        except queue.Full:
            pass

        self._writer_thread.join(timeout=15)
        if self._writer_thread.is_alive():
            print("[警告] writer thread 未正常退出", file=sys.stderr)

        self._engine.dispose()
