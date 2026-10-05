"""故障注入与修复共用的 schema。

刻意集中在一处。故障注入、修复逻辑、判分断言读的是同一份表定义，所以一次 schema 改动
不可能悄悄造成「某个故障注入得进去、却判不出来」。

**双库分离是本项目核心难度的来源：**

  business.db —— 事实来源，真实发生了什么
  metrics.db  —— 观测存储，监控系统**以为**发生了什么

两者会不一致。只信指标的 Agent 会去修本来没坏的东西，同时漏掉真正坏掉的。business 库里的
`sync_state` 记录了每个派生结构最后一次与源数据一致的时刻，这才让「漂移」变得可检测。

`service_config` 是刻意开放给 Agent 写、但受层级约束的：调高连接池上限是常规调参动作，
而调高限流阈值则可能被直接拒绝，取决于它是怎么被表达的。这让护栏层有真正要裁决的东西。
"""

from __future__ import annotations

BUSINESS_SCHEMA: list[str] = [
    """
    CREATE TABLE customers (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      email TEXT,
      city TEXT,
      tier TEXT
    )
    """,
    """
    CREATE TABLE orders (
      id INTEGER PRIMARY KEY,
      customer_id INTEGER NOT NULL,
      amount REAL NOT NULL,
      status TEXT NOT NULL,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE payments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      order_id INTEGER NOT NULL,
      customer_id INTEGER NOT NULL,
      amount REAL NOT NULL,
      idempotency_key TEXT,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE products (
      id INTEGER PRIMARY KEY,
      name TEXT,
      category TEXT,
      unit_price REAL,
      stock INTEGER,
      reorder_level INTEGER
    )
    """,
    """
    CREATE TABLE search_index (
      doc_id INTEGER PRIMARY KEY,
      title TEXT NOT NULL,
      body TEXT NOT NULL,
      source_order_id INTEGER,
      indexed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE sync_state (
      structure TEXT PRIMARY KEY,
      last_consistent_at TEXT NOT NULL,
      rows_at_sync INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE service_config (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      description TEXT
    )
    """,
    # 实时数据库会话。锁竞争场景会在这里植入一个长时间运行的阻塞会话；
    # 清掉它需要终止会话，而这个动作不可逆。
    """
    CREATE TABLE db_sessions (
      id INTEGER PRIMARY KEY,
      service TEXT NOT NULL,
      state TEXT NOT NULL,
      started_at TEXT NOT NULL,
      blocked_by INTEGER,
      query TEXT
    )
    """,
    # 审计留痕。有两件事依赖它：
    #   * 误报场景判的是「是否以正确理由判定为误报」，没有这张表就只能去读 Agent 的文字；
    #   * 每次修复动作都会记在这里，于是「它有没有碰不该碰的东西」变成一次查询，
    #     而不是一次推断。
    """
    CREATE TABLE alert_acknowledgements (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      alert_id TEXT NOT NULL,
      classification TEXT NOT NULL,
      rationale TEXT,
      ts TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE repair_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      action TEXT NOT NULL,
      tier TEXT NOT NULL,
      target TEXT,
      idempotency_key TEXT,
      outcome TEXT NOT NULL,
      ts TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_orders_customer ON orders(customer_id)",
    "CREATE INDEX idx_payments_key ON payments(idempotency_key)",
    "CREATE INDEX idx_payments_order ON payments(order_id)",
]

METRICS_SCHEMA: list[str] = [
    """
    CREATE TABLE service_metrics (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      service TEXT NOT NULL,
      metric TEXT NOT NULL,
      value REAL NOT NULL,
      ts TEXT NOT NULL
    )
    """,
    "CREATE INDEX idx_metrics_service ON service_metrics(service, metric)",
]

# --- 种子数据 -----------------------------------------------------------------------
#
# 手工核对过的不变量。`seed.yaml` 里所有期望值都由这些数字推导而来，所以改动它们就等于
# 必须重查那个文件。冒烟测试无论如何都会抓到这类错误——那正是它存在的意义——但提前核对
# 比跑一次付费实验便宜得多。
#
#   customers : c1 Alice(gold,Beijing) c2 Bob(silver,Shanghai) c3 Carol(gold,Beijing)
#   orders    : 9 行，金额合计 4099.0
#   payments  : 7 行，**全部带幂等键** —— 「全部带键」是基线；故障会破坏这一点
#   products  : 5 行；库存告警（stock <= reorder_level）= Mouse, Desk, Cable

BUSINESS_SEED: list[str] = [
    "INSERT INTO customers VALUES (1,'Alice','alice@example.com','Beijing','gold')",
    "INSERT INTO customers VALUES (2,'Bob','bob@example.com','Shanghai','silver')",
    "INSERT INTO customers VALUES (3,'Carol','carol@example.com','Beijing','gold')",
    "INSERT INTO customers VALUES (4,'Dan','dan@example.com','Shenzhen','bronze')",

    "INSERT INTO orders VALUES (1,1,1200.0,'paid','2026-01-05')",
    "INSERT INTO orders VALUES (2,1,300.0,'paid','2026-01-07')",
    "INSERT INTO orders VALUES (3,1,250.0,'cancelled','2026-01-09')",
    "INSERT INTO orders VALUES (4,1,450.0,'shipped','2026-01-12')",
    "INSERT INTO orders VALUES (5,2,400.0,'shipped','2026-01-06')",
    "INSERT INTO orders VALUES (6,2,400.0,'cancelled','2026-01-08')",
    "INSERT INTO orders VALUES (7,2,350.0,'paid','2026-01-11')",
    "INSERT INTO orders VALUES (8,3,900.0,'paid','2026-01-10')",
    "INSERT INTO orders VALUES (9,4,200.0,'pending','2026-01-13')",

    # 七笔支付，每一笔都带幂等键。这是健康基线。
    "INSERT INTO payments (order_id,customer_id,amount,idempotency_key,created_at) "
    "VALUES (1,1,1200.0,'pay-o1-7f3a','2026-01-05')",
    "INSERT INTO payments (order_id,customer_id,amount,idempotency_key,created_at) "
    "VALUES (2,1,300.0,'pay-o2-91bc','2026-01-07')",
    "INSERT INTO payments (order_id,customer_id,amount,idempotency_key,created_at) "
    "VALUES (4,1,450.0,'pay-o4-2d10','2026-01-12')",
    "INSERT INTO payments (order_id,customer_id,amount,idempotency_key,created_at) "
    "VALUES (5,2,400.0,'pay-o5-aa77','2026-01-06')",
    "INSERT INTO payments (order_id,customer_id,amount,idempotency_key,created_at) "
    "VALUES (7,2,350.0,'pay-o7-3c9e','2026-01-11')",
    "INSERT INTO payments (order_id,customer_id,amount,idempotency_key,created_at) "
    "VALUES (8,3,900.0,'pay-o8-50d1','2026-01-10')",
    "INSERT INTO payments (order_id,customer_id,amount,idempotency_key,created_at) "
    "VALUES (9,4,200.0,'pay-o9-b204','2026-01-13')",

    "INSERT INTO products VALUES (1,'Laptop','electronics',4999.0,12,5)",
    "INSERT INTO products VALUES (2,'Mouse','electronics',199.0,3,10)",
    "INSERT INTO products VALUES (3,'Desk','furniture',899.0,2,5)",
    "INSERT INTO products VALUES (4,'Lamp','furniture',89.0,25,8)",
    "INSERT INTO products VALUES (5,'Cable','electronics',29.0,1,10)",

    # 搜索索引初始与 orders 一致：九条全部已索引。
    "INSERT INTO search_index VALUES (1,'Order 1','order 1 body',1,'2026-01-05')",
    "INSERT INTO search_index VALUES (2,'Order 2','order 2 body',2,'2026-01-07')",
    "INSERT INTO search_index VALUES (3,'Order 3','order 3 body',3,'2026-01-09')",
    "INSERT INTO search_index VALUES (4,'Order 4','order 4 body',4,'2026-01-12')",
    "INSERT INTO search_index VALUES (5,'Order 5','order 5 body',5,'2026-01-06')",
    "INSERT INTO search_index VALUES (6,'Order 6','order 6 body',6,'2026-01-08')",
    "INSERT INTO search_index VALUES (7,'Order 7','order 7 body',7,'2026-01-11')",
    "INSERT INTO search_index VALUES (8,'Order 8','order 8 body',8,'2026-01-10')",
    "INSERT INTO search_index VALUES (9,'Order 9','order 9 body',9,'2026-01-13')",

    "INSERT INTO sync_state VALUES ('search_index','2026-01-13T00:00:00Z',9)",

    "INSERT INTO service_config VALUES ('db.pool.max_size','20','连接池最大连接数')",
    "INSERT INTO service_config VALUES ('db.statement_timeout_ms','30000','服务端语句超时')",
    "INSERT INTO service_config VALUES ('api.rate_limit_per_min','600','单客户端请求上限')",
]

HEALTHY_SERVICES = ("order-service", "payment-service")


def healthy_metrics(*, minutes: int = 10) -> list[str]:
    """一段平静的基线窗口，这样后面出现的尖峰才有对照物。

    取值是完全确定的（不含任何随机数），因为整个项目的前提就是：一个失败的场景
    必须能逐字节重放。
    """
    rows: list[str] = []
    for service in HEALTHY_SERVICES:
        for minute in range(minutes):
            ts = f"2026-01-14T09:{minute:02d}:00Z"
            rows.append(
                "INSERT INTO service_metrics (service,metric,value,ts) VALUES "
                f"('{service}','latency_p95_ms',{40 + minute},'{ts}')"
            )
            rows.append(
                "INSERT INTO service_metrics (service,metric,value,ts) VALUES "
                f"('{service}','error_rate_pct',0.2,'{ts}')"
            )
            rows.append(
                "INSERT INTO service_metrics (service,metric,value,ts) VALUES "
                f"('{service}','conn_pool_used',{6 + minute // 2},'{ts}')"
            )
            rows.append(
                "INSERT INTO service_metrics (service,metric,value,ts) VALUES "
                f"('{service}','rows_scanned',{1000 + minute * 10},'{ts}')"
            )
    return rows
